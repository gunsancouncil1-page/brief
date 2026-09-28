from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, Settings
from app.database import Database
from app.sections import menu_payload, section_payload


TEMPLATES = PROJECT_ROOT / "app" / "templates"
STATIC = PROJECT_ROOT / "app" / "static"

# 지난 날짜는 지우지 않고 모두 남긴다. 날짜 하나가 수십 KB라 몇 해를
# 쌓아도 GitHub Pages 한도(1GB)에 한참 못 미친다.


DATE_FILE = re.compile(r"^\d{4}-\d{2}-\d{2}\.json$")
PUBLIC_FIELDS = ("id", "title", "publisher", "published_at", "source_url", "matched_keywords", "preferred")


def sanitize_date_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """지난 날짜 파일도 공개 규칙(제목·언론사·시각·직접링크)만 남긴다.

    보관본은 git 이력이나 이전 빌드에서 가져오므로, 혹시 섞여 있을지 모를
    본문 같은 필드를 여기서 한 번 더 걷어 낸다.
    """
    sections: dict[str, Any] = {}
    for key, section in (payload.get("sections") or {}).items():
        articles = [
            {field: article.get(field) for field in PUBLIC_FIELDS}
            for article in section.get("articles") or []
        ]
        briefing = section.get("briefing")
        sections[key] = {
            "approved": True,
            "report_date": section.get("report_date") or payload.get("report_date"),
            "published_count": len(articles),
            "generate_briefing": bool(section.get("generate_briefing")),
            "articles": articles,
            "briefing": (
                {"body": briefing.get("body", ""), "status": briefing.get("status", "complete")}
                if briefing
                else None
            ),
        }
    return {"report_date": payload.get("report_date"), "sections": sections}


def archived_dates(data_dir: Path) -> list[str]:
    """보관된 날짜들. 최신이 앞에 온다."""
    if not data_dir.is_dir():
        return []
    return sorted(
        (path.stem for path in data_dir.iterdir() if DATE_FILE.match(path.name)),
        reverse=True,
    )


def import_archive(source: Path, data_dir: Path) -> list[str]:
    """이미 게시된 날짜 파일 가운데 여기 없는 것을 들여온다.

    이 PC의 빌드 폴더가 지워져도, GitHub Pages에 올라가 있는 지난 자료로
    다시 채울 수 있게 한다.
    """
    imported: list[str] = []
    for report_date in archived_dates(source):
        target = data_dir / f"{report_date}.json"
        if target.exists():
            continue
        try:
            payload = json.loads((source / f"{report_date}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        target.write_text(
            json.dumps(sanitize_date_payload(payload), ensure_ascii=False), encoding="utf-8"
        )
        imported.append(report_date)
    return imported


def public_article(article: dict[str, Any]) -> dict[str, Any]:
    """공개 화면과 같은 규칙: 제목·언론사·발행시각·원문 직접링크만."""
    return {
        "id": article["id"],
        "title": article["title"],
        "publisher": article["publisher"],
        "published_at": article["published_at"],
        "source_url": article["source_url"],
        "matched_keywords": article["matched_keywords"],
        "preferred": article["preferred"],
    }


def date_payload(database: Database, report_date: str) -> dict[str, Any]:
    """한 날짜의 승인된 결과만 모은다. 승인 전 자료는 담지 않는다."""
    sections: dict[str, Any] = {}
    for job in database.jobs(report_date=report_date):
        if not job["approved_at"]:
            continue
        articles = database.articles(job["id"], unique_only=True, include_excluded=False)
        briefing = database.get_briefing(job["id"]) if job["generate_briefing"] else None
        sections[job["section"]] = {
            "approved": True,
            "report_date": job["report_date"],
            "published_count": len(articles),
            "generate_briefing": job["generate_briefing"],
            "articles": [public_article(article) for article in articles],
            "briefing": {"body": briefing["body"], "status": briefing["status"]} if briefing else None,
        }
    return {"report_date": report_date, "sections": sections}


def _static_index_html() -> str:
    """서버용 화면을 그대로 쓰되, 정적 사이트에 맞게 주소만 바꾼다."""
    html = (TEMPLATES / "index.html").read_text(encoding="utf-8")
    html = html.replace('href="/static/styles.css?v={{ASSET_VERSION}}"', 'href="./styles.css"')
    html = html.replace('src="/static/app.js?v={{ASSET_VERSION}}"', 'src="./app.js"')
    # 정적 화면임을 알려 주면 app.js가 API 대신 JSON 파일을 읽는다.
    html = html.replace("<body>", '<body data-mode="static">')
    # 관리자 페이지와 서버 상태는 이 PC에만 있다. 공개 사이트에서는 링크를 뺀다.
    html = html.replace(
        """        <span class="footer-links">
          <a href="/admin">관리자</a>
          <a href="/health" target="_blank" rel="noreferrer">서버 상태</a>
        </span>""",
        """        <span class="footer-links" id="buildStamp"></span>""",
    )
    return html


def build_site(
    database: Database,
    settings: Settings,
    destination: Path,
    *,
    archive_dir: Path | None = None,
) -> dict[str, Any]:
    """승인된 결과를 정적 사이트로 내보낸다. GitHub Pages가 그대로 서비스한다.

    날짜별 파일은 지우지 않고 쌓아 둔다. 관리자 쪽 자료(DB)는 오늘 몫만 남기고
    정리하지만, 공개 사이트에서는 지난 날짜를 골라 다시 볼 수 있어야 한다.
    """
    destination.mkdir(parents=True, exist_ok=True)
    data_dir = destination / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if archive_dir is not None:
        import_archive(archive_dir, data_dir)

    for report_date in database.dates():
        target = data_dir / f"{report_date}.json"
        payload = date_payload(database, report_date)
        if not payload["sections"]:
            # 승인된 것이 하나도 없는 날짜(검토 대기, 공개 내림)는 올리지 않는다.
            target.unlink(missing_ok=True)
            continue
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    published_dates = archived_dates(data_dir)

    now = datetime.now(settings.timezone)
    index = {
        "menu": menu_payload(),
        "sections": section_payload(database.section_review_flags()),
        "collect_at": "05:00",
        "today": now.date().isoformat(),
        "latest_date": published_dates[0] if published_dates else None,
        "dates": published_dates,
        "built_at": now.isoformat(timespec="minutes"),
    }
    (data_dir / "index.json").write_text(json.dumps(index, ensure_ascii=False), encoding="utf-8")

    (destination / "index.html").write_text(_static_index_html(), encoding="utf-8")
    shutil.copyfile(STATIC / "styles.css", destination / "styles.css")
    shutil.copyfile(STATIC / "app.js", destination / "app.js")
    # Jekyll 처리를 건너뛰게 해 파일이 그대로 올라가도록 한다.
    (destination / ".nojekyll").write_text("", encoding="utf-8")

    return {
        "destination": str(destination),
        "dates": published_dates,
        "latest_date": index["latest_date"],
        "built_at": index["built_at"],
    }
