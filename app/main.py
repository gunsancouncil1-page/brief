from __future__ import annotations

import asyncio
import logging
import secrets
import shutil
import sqlite3
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from app.config import PROJECT_ROOT, Settings, load_settings
from app.database import Database
from app.security import SESSION_COOKIE, issue_session, verify_session
from app.sections import MENU, SECTIONS, menu_payload, section_payload
from app.services.crawler import MATCH_MODES, SearchSpec
from app.services.job_runner import (
    JobRunner,
    is_collection_day,
    report_window,
    requires_review,
)
from app.services.publisher import PublishError, pages_url, publish


TEMPLATES = PROJECT_ROOT / "app" / "templates"
STATIC_DIR = PROJECT_ROOT / "app" / "static"


def render_page(name: str) -> HTMLResponse:
    """정적 자원 주소에 최종 수정 시각을 붙여 내려준다.

    브라우저가 예전 CSS/JS를 붙들고 있으면 바뀐 화면이 그대로 보이지 않는다.
    파일이 바뀌면 주소가 바뀌므로 새로고침만으로 반영된다.
    """
    version = max(
        (path.stat().st_mtime_ns for path in STATIC_DIR.glob("*") if path.is_file()), default=0
    )
    html = (TEMPLATES / name).read_text(encoding="utf-8")
    return HTMLResponse(html.replace("{{ASSET_VERSION}}", str(version)[-12:]))


COLLECT_HOUR = 5
COLLECT_MINUTE = 0


def _valid_date(value: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise HTTPException(status_code=400, detail="날짜는 YYYY-MM-DD 형식이어야 합니다.") from error


def _clean_keywords(values: list[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        keyword = " ".join(value.split())
        if keyword and keyword not in seen:
            seen.append(keyword)
    return seen


class JobRequest(BaseModel):
    report_date: str
    section: str
    # 비우면 갈래의 기본 검색 조건을 그대로 쓴다.
    keywords: list[str] = Field(default_factory=list, max_length=20)
    exclude_keywords: list[str] = Field(default_factory=list, max_length=20)
    match_mode: str = ""
    generate_briefing: bool | None = None

    @field_validator("section")
    @classmethod
    def _check_section(cls, value: str) -> str:
        if value not in SECTIONS:
            raise ValueError(f"section은 {', '.join(SECTIONS)} 중 하나여야 합니다.")
        return value

    @field_validator("keywords", "exclude_keywords")
    @classmethod
    def _check_keywords(cls, value: list[str]) -> list[str]:
        cleaned = _clean_keywords(value)
        if any(len(keyword) > 40 for keyword in cleaned):
            raise ValueError("키워드는 40자 이하여야 합니다.")
        return cleaned

    @field_validator("match_mode")
    @classmethod
    def _check_match_mode(cls, value: str) -> str:
        if value and value not in MATCH_MODES:
            raise ValueError("match_mode는 any 또는 all이어야 합니다.")
        return value


class BulkJobRequest(BaseModel):
    report_date: str
    sections: list[str] = Field(default_factory=lambda: list(SECTIONS))


class ApprovalRequest(BaseModel):
    # 공개에서 뺄 기사. 나머지는 모두 공개된다.
    excluded_ids: list[str] = Field(default_factory=list)
    # 관리자가 정한 노출 순서. 비우면 기본 순서(지역 매체 → 최신순)를 쓴다.
    ordered_ids: list[str] = Field(default_factory=list)


class LinkedArticleRequest(BaseModel):
    # 관리자가 검토 화면에서 붙여 넣는 기사 주소.
    url: str


class SectionReviewRequest(BaseModel):
    # True면 관리자 승인 후 공개, False면 수집 즉시 공개.
    requires_review: bool


class LoginRequest(BaseModel):
    key: str


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    database = Database(settings.database_path)
    runner = JobRunner(database, settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        database.initialize()
        database.reset_interrupted_jobs()
        if settings.host not in {"127.0.0.1", "localhost"} and len(settings.admin_api_key) < 24:
            # 이 PC 밖에서 열리는데 관리자 키가 짧으면 그대로 위험이 된다.
            logging.getLogger("uvicorn.error").warning(
                "관리자 페이지가 %s 에서 열립니다. ADMIN_API_KEY를 더 긴 값으로 바꾸세요.",
                settings.host,
            )
        if settings.auto_register:
            # 05:00에 PC가 꺼져 있었더라도, 켜는 시점에 그날 몫을 채우고 이어서 돌린다.
            runner.ensure_registered(datetime.now(settings.timezone).date())
            asyncio.get_running_loop().create_task(runner.run_due_jobs())
        scheduler: AsyncIOScheduler | None = None
        if settings.schedule_enabled:
            scheduler = AsyncIOScheduler(timezone=settings.timezone)
            scheduler.add_job(
                runner.collect_today,
                trigger=CronTrigger(
                    hour=COLLECT_HOUR, minute=COLLECT_MINUTE, timezone=settings.timezone
                ),
                id="gunsan-scheduled-jobs",
                replace_existing=True,
                coalesce=True,
                misfire_grace_time=60 * 60 * 6,
            )
            scheduler.start()
        app.state.scheduler = scheduler
        yield
        if scheduler:
            scheduler.shutdown(wait=False)

    app = FastAPI(title="군산 보도자료 스크랩", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.settings = settings
    app.state.database = database
    app.state.runner = runner

    # -- authentication --------------------------------------------------

    def require_admin(
        x_admin_key: Annotated[str, Header()] = "",
        session: Annotated[str, Cookie(alias=SESSION_COOKIE)] = "",
    ) -> bool:
        if not settings.admin_api_key:
            raise HTTPException(status_code=503, detail="ADMIN_API_KEY가 설정되어 있지 않습니다.")
        if x_admin_key and secrets.compare_digest(x_admin_key, settings.admin_api_key):
            return True
        if verify_session(settings.admin_api_key, session):
            return True
        raise HTTPException(status_code=401, detail="관리자 인증이 필요합니다.")

    @app.middleware("http")
    async def origin_secret_guard(request: Request, call_next):
        # When a Worker proxy is enabled, protect the tunnel origin from being
        # opened directly. Keep this blank during local browser development.
        if settings.origin_shared_secret and request.url.path != "/health":
            supplied = request.headers.get("X-Gunsan-Origin-Key", "")
            if not secrets.compare_digest(supplied, settings.origin_shared_secret):
                return JSONResponse(status_code=403, content={"detail": "Origin authentication required"})
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        # 화면과 정적 자원은 배포 즉시 반영돼야 한다. 브라우저가 예전 CSS를 붙들고
        # 있으면 바뀐 디자인이 보이지 않는다. 기사 사진은 캐시해도 무방하다.
        if request.url.path in {"/", "/admin"} or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")

    # -- pages -----------------------------------------------------------

    @app.get("/")
    async def index():
        return render_page("index.html")

    @app.get("/admin")
    async def admin_page():
        return render_page("admin.html")

    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "scheduler": bool(app.state.scheduler),
            "collect_at": f"{COLLECT_HOUR:02d}:{COLLECT_MINUTE:02d}",
        }

    # -- read-only API ---------------------------------------------------

    def _job_or_404(job_id: str) -> dict[str, Any]:
        job = database.get_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 작업입니다.")
        return job

    def _section_job_or_404(report_date: str, section: str) -> dict[str, Any]:
        if section not in SECTIONS:
            raise HTTPException(status_code=404, detail="알 수 없는 메뉴입니다.")
        job = database.job_for_section(_valid_date(report_date), section)
        if job is None:
            raise HTTPException(status_code=404, detail="이 날짜에 등록된 수집이 없습니다.")
        return job

    def _public_article(article: dict[str, Any]) -> dict[str, Any]:
        """공개 화면에는 제목·언론사·발행시각과 원문 직접링크만 내보낸다.

        본문과 사진을 우리 쪽에서 다시 보여 주면 복제·전송에 해당한다.
        읽기는 각 언론사 원문 페이지에서 이뤄지도록 한다.
        """
        return {
            "id": article["id"],
            "title": article["title"],
            "publisher": article["publisher"],
            "published_at": article["published_at"],
            "source_url": article["source_url"],
            "matched_keywords": article["matched_keywords"],
            "preferred": article["preferred"],
        }

    def _published_job_or_404(report_date: str, section: str) -> dict[str, Any]:
        """공개 화면은 관리자가 승인한 결과만 보여 준다."""
        job = _section_job_or_404(report_date, section)
        if not job["approved_at"]:
            raise HTTPException(status_code=404, detail="아직 승인되지 않았습니다.")
        return job

    @app.get("/api/menu")
    async def menu():
        # 공개 화면은 가장 최근 수집분 하나만 보여 준다.
        dates = database.dates()
        return {
            "menu": menu_payload(),
            "sections": section_payload(database.section_review_flags()),
            "collect_at": f"{COLLECT_HOUR:02d}:{COLLECT_MINUTE:02d}",
            "today": datetime.now(settings.timezone).date().isoformat(),
            "latest_date": dates[0] if dates else None,
        }

    @app.get("/api/dates")
    async def list_dates():
        return {"dates": database.dates()}

    @app.get("/api/reports/{report_date}")
    async def report_overview(report_date: str):
        """한 날짜의 갈래별 수집 상태. 화면 메뉴가 이 응답으로 그려진다."""
        report_date = _valid_date(report_date)
        jobs = {}
        for job in database.jobs(report_date=report_date):
            job["approved"] = bool(job["approved_at"])
            # 공개 화면에는 승인 뒤 실제로 남은 건수를 보여 준다.
            job["published_count"] = (
                len(database.articles(job["id"], unique_only=True, include_excluded=False))
                if job["approved"]
                else 0
            )
            jobs[job["section"]] = job
        return {
            "report_date": report_date,
            "jobs": {key: jobs.get(key) for key in SECTIONS},
        }

    @app.get("/api/reports/{report_date}/{section}/articles")
    async def section_articles(report_date: str, section: str, view: str = "unique"):
        job = _published_job_or_404(report_date, section)
        if view not in {"all", "unique"}:
            raise HTTPException(status_code=400, detail="view는 all 또는 unique여야 합니다.")
        articles = database.articles(
            job["id"], unique_only=view == "unique", include_excluded=False
        )
        return {"job": job, "articles": [_public_article(article) for article in articles]}

    @app.get("/api/reports/{report_date}/{section}/briefing")
    async def section_briefing(report_date: str, section: str):
        job = _published_job_or_404(report_date, section)
        briefing = database.get_briefing(job["id"])
        if not briefing:
            raise HTTPException(status_code=404, detail="아직 생성된 브리핑이 없습니다.")
        return {"job": job, "briefing": briefing}

    @app.get("/api/jobs")
    async def list_jobs(report_date: str | None = None):
        if report_date:
            report_date = _valid_date(report_date)
        return {"jobs": database.jobs(report_date=report_date)}

    @app.get("/api/jobs/{job_id}")
    async def get_job(job_id: str):
        return {"job": _job_or_404(job_id)}

    @app.get("/api/jobs/{job_id}/articles")
    async def list_articles(job_id: str, view: str = "all"):
        job = _job_or_404(job_id)
        if view not in {"all", "unique"}:
            raise HTTPException(status_code=400, detail="view는 all 또는 unique여야 합니다.")
        return {
            "job": job,
            "articles": database.articles(job_id, unique_only=view == "unique"),
        }

    @app.get("/api/images/{image_id}")
    async def article_image(image_id: str):
        image = database.get_image(image_id)
        if image is None:
            raise HTTPException(status_code=404, detail="등록되지 않은 이미지입니다.")
        path = Path(image["path"])
        if not path.is_file():
            raise HTTPException(status_code=404, detail="저장된 이미지 파일이 없습니다.")
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/api/jobs/{job_id}/briefing")
    async def get_briefing(job_id: str):
        job = _job_or_404(job_id)
        briefing = database.get_briefing(job_id)
        if not briefing:
            raise HTTPException(status_code=404, detail="아직 생성된 브리핑이 없습니다.")
        return {"job": job, "briefing": briefing}

    # -- admin API -------------------------------------------------------

    @app.post("/api/admin/login")
    async def admin_login(payload: LoginRequest, response: Response):
        if not settings.admin_api_key:
            raise HTTPException(status_code=503, detail="ADMIN_API_KEY가 설정되어 있지 않습니다.")
        if not secrets.compare_digest(payload.key, settings.admin_api_key):
            raise HTTPException(status_code=401, detail="관리자 키가 올바르지 않습니다.")
        token, max_age = issue_session(settings.admin_api_key, settings.admin_session_hours)
        response.set_cookie(
            SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="strict", path="/"
        )
        return {"authenticated": True, "expires_in": max_age}

    @app.post("/api/admin/logout")
    async def admin_logout(response: Response):
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"authenticated": False}

    @app.get("/api/admin/session")
    async def admin_session(
        session: Annotated[str, Cookie(alias=SESSION_COOKIE)] = "",
    ):
        return {
            "authenticated": verify_session(settings.admin_api_key, session),
            "configured": bool(settings.admin_api_key),
            "sections": _section_list(),
            "collect_at": f"{COLLECT_HOUR:02d}:{COLLECT_MINUTE:02d}",
            "timezone": str(settings.timezone),
            "scheduler": bool(app.state.scheduler),
            "auto_register": settings.auto_register,
            "pages_url": pages_url(),
            "today": datetime.now(settings.timezone).date().isoformat(),
            "busy": runner.busy,
        }

    def _section_list() -> list[dict[str, Any]]:
        """관리자 화면에 쓰는 갈래 목록. 메뉴 차례를 그대로 따른다."""
        payload = section_payload(database.section_review_flags())
        return [payload[tab.section] for tab in MENU if tab.view == "articles"]

    def _decorate(job: dict[str, Any]) -> dict[str, Any]:
        job["runnable_now"] = (
            job["status"] in {"pending", "failed", "complete"}
            and datetime.fromisoformat(job["window_end"]) <= datetime.now(settings.timezone)
        )
        job["query_preview"] = SearchSpec.from_job(job).query
        job["section_label"] = SECTIONS[job["section"]].label if job["section"] in SECTIONS else job["name"]
        job["approved"] = bool(job["approved_at"])
        job["requires_review"] = requires_review(job, database.section_review_flags())
        job["needs_review"] = (
            job["requires_review"] and job["status"] == "complete" and not job["approved_at"]
        )
        return job

    @app.get("/api/admin/jobs")
    async def admin_jobs(report_date: str | None = None, _: bool = Depends(require_admin)):
        if report_date:
            report_date = _valid_date(report_date)
        jobs = [_decorate(job) for job in database.jobs(report_date=report_date)]
        return {"jobs": jobs, "busy": runner.busy}

    def _register(payload: JobRequest) -> dict[str, Any]:
        report_date = _valid_date(payload.report_date)
        section = SECTIONS[payload.section]
        keywords = payload.keywords or list(section.keywords)
        exclude_keywords = payload.exclude_keywords or list(section.exclude_keywords)
        match_mode = payload.match_mode or section.match_mode
        generate_briefing = (
            section.has_briefing if payload.generate_briefing is None else payload.generate_briefing
        )
        if not keywords:
            raise HTTPException(status_code=400, detail="검색 키워드를 1개 이상 선택하세요.")
        overlap = set(keywords) & set(exclude_keywords)
        if overlap:
            raise HTTPException(
                status_code=400, detail=f"같은 키워드를 포함과 제외에 함께 쓸 수 없습니다: {', '.join(overlap)}"
            )
        chosen = date.fromisoformat(report_date)
        if not is_collection_day(chosen):
            raise HTTPException(
                status_code=400,
                detail="토요일·일요일은 수집하지 않습니다. 월요일 수집이 금·토·일 사흘치를 담습니다.",
            )
        if database.job_for_section(report_date, section.key) is not None:
            raise HTTPException(
                status_code=409, detail=f"{report_date}의 '{section.label}' 수집은 이미 등록되어 있습니다."
            )

        start, end = report_window(chosen, settings.timezone)
        try:
            job = database.create_job(
                report_date=report_date,
                section=section.key,
                name=section.label,
                keywords=keywords,
                exclude_keywords=exclude_keywords,
                sites=list(section.sites),
                preferred_sites=list(section.preferred_sites),
                match_mode=match_mode,
                generate_briefing=generate_briefing,
                window_start=start.isoformat(),
                window_end=end.isoformat(),
            )
        except sqlite3.IntegrityError as error:
            raise HTTPException(
                status_code=409, detail=f"{report_date}의 '{section.label}' 수집은 이미 등록되어 있습니다."
            ) from error
        return _decorate(job)

    @app.post("/api/admin/jobs", status_code=201)
    async def create_job(payload: JobRequest, _: bool = Depends(require_admin)):
        return {"job": _register(payload)}

    @app.post("/api/admin/jobs/bulk", status_code=201)
    async def create_jobs_for_all_sections(
        payload: BulkJobRequest, _: bool = Depends(require_admin)
    ):
        """선택한 날짜에 여러 갈래를 기본 검색 조건으로 한 번에 등록한다."""
        if not is_collection_day(date.fromisoformat(_valid_date(payload.report_date))):
            raise HTTPException(
                status_code=400,
                detail="토요일·일요일은 수집하지 않습니다. 월요일 수집이 금·토·일 사흘치를 담습니다.",
            )
        created: list[dict[str, Any]] = []
        skipped: list[str] = []
        for key in payload.sections:
            if key not in SECTIONS:
                raise HTTPException(status_code=400, detail=f"알 수 없는 메뉴입니다: {key}")
            try:
                created.append(_register(JobRequest(report_date=payload.report_date, section=key)))
            except HTTPException as error:
                # 이미 등록된 갈래만 건너뛴다. 다른 오류는 그대로 알린다.
                if error.status_code != 409:
                    raise
                skipped.append(SECTIONS[key].label)
        return {"created": created, "skipped": skipped}

    @app.delete("/api/admin/jobs/{job_id}")
    async def delete_job(job_id: str, _: bool = Depends(require_admin)):
        job = _job_or_404(job_id)
        if not database.delete_job(job_id):
            raise HTTPException(status_code=404, detail="등록되지 않은 작업입니다.")
        shutil.rmtree(settings.media_dir / job["report_date"] / job_id, ignore_errors=True)
        return {"deleted": job_id}

    @app.post("/api/admin/jobs/{job_id}/run")
    async def run_job_now(job_id: str, _: bool = Depends(require_admin)):
        _job_or_404(job_id)
        try:
            return await runner.run_job(job_id)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.get("/api/admin/jobs/{job_id}/articles")
    async def admin_job_articles(job_id: str, _: bool = Depends(require_admin)):
        """검토 화면용. 중복으로 묶인 기사와 제외 표시까지 그대로 넘긴다."""
        job = _job_or_404(job_id)
        return {"job": _decorate(job), "articles": database.articles(job_id)}

    @app.post("/api/admin/jobs/{job_id}/articles", status_code=201)
    async def add_linked_article(
        job_id: str, payload: LinkedArticleRequest, _: bool = Depends(require_admin)
    ):
        """검토 화면에서 붙여 넣은 기사 주소를 스크랩에 더한다."""
        _job_or_404(job_id)
        try:
            return await runner.add_linked_article(job_id, payload.url)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/admin/jobs/{job_id}/approve")
    async def approve_job(
        job_id: str, payload: ApprovalRequest, _: bool = Depends(require_admin)
    ):
        _job_or_404(job_id)
        try:
            return await runner.approve(job_id, payload.excluded_ids, payload.ordered_ids)
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post("/api/admin/jobs/{job_id}/unapprove")
    async def unapprove_job(job_id: str, _: bool = Depends(require_admin)):
        _job_or_404(job_id)
        return runner.unapprove(job_id)

    @app.get("/api/admin/sections")
    async def admin_sections(_: bool = Depends(require_admin)):
        return {"sections": _section_list()}

    @app.post("/api/admin/sections/{section}")
    async def set_section_review(
        section: str, payload: SectionReviewRequest, _: bool = Depends(require_admin)
    ):
        """메뉴 하나의 공개 방식을 바꾼다. 다음 수집부터 이 값을 따른다."""
        if section not in SECTIONS:
            raise HTTPException(status_code=404, detail="없는 메뉴입니다.")
        database.set_section_review(section, payload.requires_review)
        payload_sections = section_payload(database.section_review_flags())
        return {"section": payload_sections[section]}

    @app.post("/api/admin/publish")
    async def publish_site(_: bool = Depends(require_admin)):
        """승인된 결과를 정적 사이트로 만들어 GitHub Pages에 올린다."""
        try:
            return await asyncio.to_thread(publish, database, settings)
        except PublishError as error:
            raise HTTPException(status_code=502, detail=str(error)) from error

    @app.post("/api/admin/run-due")
    async def run_due(_: bool = Depends(require_admin)):
        try:
            return await runner.run_due_jobs()
        except RuntimeError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    return app


app = create_app()
