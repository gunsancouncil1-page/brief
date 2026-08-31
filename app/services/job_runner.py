from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.database import Database
from app.sections import SECTIONS
from app.services.briefing import BriefingService
from app.services.crawler import (
    DuplicateDetector,
    GoogleNewsRssCollector,
    SearchSpec,
    fetch_linked_article,
)


def requires_review(job: dict[str, Any]) -> bool:
    """검토가 필요한 갈래인지. 정의에 없는 갈래는 안전하게 검토 대상으로 본다."""
    section = SECTIONS.get(job["section"])
    return section.requires_review if section else True


SATURDAY, SUNDAY = 5, 6


def is_collection_day(report_date: date) -> bool:
    """주말에는 수집하지 않는다."""
    return report_date.weekday() not in (SATURDAY, SUNDAY)


def report_window(report_date: date, timezone) -> tuple[datetime, datetime]:
    """`report_date`의 05:00을 끝으로 하는 수집 시간창.

    평일은 전날 09:00부터다. 다만 월요일은 주말 이틀을 건너뛰므로 금요일 09:00까지
    거슬러 올라가, 금·토·일 사흘치를 한 번에 담는다.
    """
    days_back = 3 if report_date.weekday() == 0 else 1
    start = datetime.combine(report_date - timedelta(days=days_back), time(9, 0), tzinfo=timezone)
    end = datetime.combine(report_date, time(5, 0), tzinfo=timezone)
    return start, end


class JobRunner:
    """관리자가 등록한 수집 작업 하나를 실제로 수행한다."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.collector = GoogleNewsRssCollector(settings)
        self.duplicate_detector = DuplicateDetector()
        self.briefing_service = BriefingService(settings)
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    def job_dir(self, job: dict[str, Any]) -> Path:
        return self.settings.media_dir / job["report_date"] / job["id"]

    def media_dir(self, job: dict[str, Any]) -> Path:
        return self.job_dir(job) / "images"

    def ensure_registered(self, report_date: date) -> list[str]:
        """그 날짜의 수집이 등록돼 있지 않으면 갈래 기본값으로 채워 넣는다.

        관리자가 매일 손으로 등록하지 않아도 평일 아침 수집이 돌아가게 한다.
        이미 등록된 갈래는 관리자가 고쳐 둔 조건을 그대로 둔다.
        """
        if not is_collection_day(report_date):
            return []
        start, end = report_window(report_date, self.settings.timezone)
        created: list[str] = []
        for section in SECTIONS.values():
            if self.database.job_for_section(report_date.isoformat(), section.key):
                continue
            self.database.create_job(
                report_date=report_date.isoformat(),
                section=section.key,
                name=section.label,
                keywords=list(section.keywords),
                exclude_keywords=list(section.exclude_keywords),
                sites=list(section.sites),
                preferred_sites=list(section.preferred_sites),
                match_mode=section.match_mode,
                generate_briefing=section.has_briefing,
                window_start=start.isoformat(),
                window_end=end.isoformat(),
            )
            created.append(section.key)
        return created

    async def collect_today(self, now: datetime | None = None) -> dict[str, Any]:
        """05:00 스케줄러가 부르는 일과. 등록을 채운 뒤 밀린 수집까지 처리한다."""
        moment = now or datetime.now(self.settings.timezone)
        registered = (
            self.ensure_registered(moment.date()) if self.settings.auto_register else []
        )
        result = await self.run_due_jobs(moment)
        result["registered"] = registered
        return result

    async def run_due_jobs(self, now: datetime | None = None) -> dict[str, Any]:
        """05:00 스케줄러가 호출한다. 시간창이 닫힌 대기 작업만 실행한다."""
        moment = now or datetime.now(self.settings.timezone)
        due = self.database.due_jobs(moment.isoformat())
        results = []
        for job in due:
            if not is_collection_day(date.fromisoformat(job["report_date"])):
                results.append({"job_id": job["id"], "status": "skipped", "error": "주말은 수집하지 않습니다."})
                continue
            try:
                results.append(await self.run_job(job["id"]))
            except RuntimeError as error:
                # Another run holds the lock. The job stays pending, so the next
                # 05:00 pass (or a manual run) picks it up instead of the batch
                # stopping here.
                results.append({"job_id": job["id"], "status": "skipped", "error": str(error)})
        return {"ran_at": moment.isoformat(), "job_count": len(results), "jobs": results}

    async def run_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get_job(job_id)
        if job is None:
            raise LookupError("Unknown job")
        if self._lock.locked():
            raise RuntimeError("A collection run is already in progress")

        async with self._lock:
            self.database.update_job_run(job_id, status="running", error_message=None)
            try:
                result = await self._collect(job)
            except Exception as error:  # noqa: BLE001 - recorded on the job row
                message = f"{type(error).__name__}: {error}"
                existing = self.database.articles(job_id)
                unique_existing = self.database.articles(job_id, unique_only=True)
                self.database.update_job_run(
                    job_id,
                    status="failed",
                    article_count=len(existing),
                    unique_count=len(unique_existing),
                    error_message=message,
                    last_run_at=datetime.now(UTC).isoformat(),
                )
                return {"job_id": job_id, "status": "failed", "error": message}
            return result

    def _prune_media(self, job: dict[str, Any]) -> None:
        """더 이상 참조되지 않는 이미지 파일을 지운다.

        일부 언론사는 이미지 주소에 매번 다른 쿼리 문자열을 붙인다. 재실행 때마다
        새 파일이 쌓이지 않도록, DB가 가리키지 않는 파일은 정리한다.
        """
        media_dir = self.media_dir(job)
        if not media_dir.is_dir():
            return
        referenced = {
            Path(image["path"]).name
            for images in self.database.images_by_article(job["id"]).values()
            for image in images
        }
        for stored in media_dir.glob("*.jpg"):
            if stored.name not in referenced:
                stored.unlink(missing_ok=True)

    async def _collect(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = job["id"]
        spec = SearchSpec.from_job(job)
        start = datetime.fromisoformat(job["window_start"])
        end = datetime.fromisoformat(job["window_end"])

        candidates = await self.collector.collect(
            spec,
            job_id=job_id,
            start=start,
            end=end,
            media_dir=self.media_dir(job) if self.settings.images_enabled else None,
        )
        for candidate in candidates:
            images = candidate.pop("images", [])
            self.database.upsert_article(candidate)
            # The article row must exist first: images reference it.
            self.database.replace_article_images(candidate["id"], images)

        self._prune_media(job)
        total_count, unique_count = self.duplicate_detector.mark(self.database, job_id)

        # 새로 수집한 결과는 일단 비공개로 되돌린다. 검토가 필요한 갈래는 여기서 멈추고,
        # 그렇지 않은 갈래는 이어서 바로 공개한다.
        self.database.set_approved(job_id, False)
        self.database.update_job_run(
            job_id,
            status="complete",
            article_count=total_count,
            unique_count=unique_count,
            error_message=None,
            last_run_at=datetime.now(UTC).isoformat(),
        )

        result = {
            "job_id": job_id,
            "status": "complete",
            "approved": False,
            "article_count": total_count,
            "unique_count": unique_count,
        }
        if not requires_review(job):
            # 관리자가 예전에 빼 둔 기사가 있으면 그대로 유지한 채 다시 공개한다.
            kept_out = [
                article["id"] for article in self.database.articles(job_id) if article["excluded"]
            ]
            kept_order = [
                article["id"]
                for article in self.database.articles(job_id)
                if article["sort_order"]
            ]
            approval = await self.approve(job_id, kept_out, kept_order)
            result["approved"] = True
            result["published_count"] = approval["published_count"]
            result["briefing_status"] = approval["briefing_status"]
        return result

    async def add_linked_article(self, job_id: str, url: str) -> dict[str, Any]:
        """관리자가 붙여 넣은 기사 주소를 그 날짜의 스크랩에 더한다.

        검색이 놓친 기사를 관리자가 직접 채워 넣는 통로다. 키워드 조건과 매체
        제한은 적용하지 않고, 중복으로 묶여 사라지지도 않는다.
        """
        job = self.database.get_job(job_id)
        if job is None:
            raise LookupError("Unknown job")
        if job["status"] == "running":
            raise RuntimeError("수집이 도는 중에는 기사를 추가할 수 없습니다.")

        url = (url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise ValueError("http로 시작하는 기사 주소를 넣어 주세요.")

        headers = {"User-Agent": self.settings.user_agent}
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(
            headers=headers, timeout=timeout, follow_redirects=True
        ) as client:
            try:
                article = await fetch_linked_article(
                    client,
                    url,
                    job_id=job_id,
                    report_date=job["report_date"],
                    timezone=self.settings.timezone,
                )
            except httpx.HTTPError as error:
                raise ValueError(f"기사 주소를 여는 데 실패했습니다: {error}") from error

        existing = {item["source_url"]: item for item in self.database.articles(job_id)}
        already = existing.get(article["source_url"]) is not None

        article.pop("images", None)
        self.database.upsert_article(article)
        total_count, unique_count = self.duplicate_detector.mark(self.database, job_id)
        self.database.update_job_run(
            job_id,
            status=job["status"],
            article_count=total_count,
            unique_count=unique_count,
            error_message=job["error_message"],
            last_run_at=job["last_run_at"],
        )

        saved = next(
            (item for item in self.database.articles(job_id) if item["id"] == article["id"]),
            None,
        )
        return {
            "job_id": job_id,
            "article": saved,
            "already_present": already,
            "article_count": total_count,
            "unique_count": unique_count,
            # 이미 공개된 갈래라면 기사는 바로 보이지만 브리핑은 예전 것 그대로다.
            "approved": bool(job["approved_at"]),
        }


    async def approve(
        self,
        job_id: str,
        excluded_ids: list[str],
        ordered_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """관리자가 뺀 기사와 정한 순서를 반영하고, 남은 기사로 브리핑을 만든 뒤 공개한다.

        브리핑은 이 시점에 만든다. 수집 직후에 만들면 관리자가 뺀 기사까지
        요약에 섞이기 때문이다.
        """
        job = self.database.get_job(job_id)
        if job is None:
            raise LookupError("Unknown job")
        if job["status"] != "complete":
            raise RuntimeError("수집이 끝난 작업만 승인할 수 있습니다.")

        excluded_count = self.database.set_excluded_articles(job_id, excluded_ids)
        ordered_count = self.database.set_article_order(job_id, ordered_ids or [])
        published = self.database.articles(job_id, unique_only=True, include_excluded=False)

        briefing_status = "skipped"
        if job["generate_briefing"]:
            briefing = await self.briefing_service.create(job, published)
            self.database.save_briefing(job_id, **briefing)
            briefing_status = briefing["status"]

        self.database.set_approved(job_id, True)
        return {
            "job_id": job_id,
            "approved": True,
            "published_count": len(published),
            "excluded_count": excluded_count,
            "ordered_count": ordered_count,
            "briefing_status": briefing_status,
        }

    def unapprove(self, job_id: str) -> dict[str, Any]:
        """공개를 내리고 다시 검토 상태로 되돌린다."""
        if self.database.get_job(job_id) is None:
            raise LookupError("Unknown job")
        self.database.set_approved(job_id, False)
        return {"job_id": job_id, "approved": False}
