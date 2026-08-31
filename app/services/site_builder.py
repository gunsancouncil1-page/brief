from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, Settings
from app.database import Database
from app.sections import menu_payload, section_payload


TEMPLATES = PROJECT_ROOT / "app" / "templates"
STATIC = PROJECT_ROOT / "app" / "static"

# 정적 사이트에 실을 날짜 수. 지난 자료는 이 개수만큼만 함께 올라간다.
MAX_DATES = 30


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


def build_site(database: Database, settings: Settings, destination: Path) -> dict[str, Any]:
    """승인된 결과를 정적 사이트로 내보낸다. GitHub Pages가 그대로 서비스한다."""
    destination.mkdir(parents=True, exist_ok=True)
    data_dir = destination / "data"
    if data_dir.exists():
        shutil.rmtree(data_dir)
    data_dir.mkdir(parents=True)

    dates = database.dates()[:MAX_DATES]
    published_dates: list[str] = []
    for report_date in dates:
        payload = date_payload(database, report_date)
        if not payload["sections"]:
            continue  # 승인된 것이 하나도 없는 날짜는 올리지 않는다
        (data_dir / f"{report_date}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        published_dates.append(report_date)

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
