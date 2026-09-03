import asyncio
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.main import create_app
from app.sections import LOCAL_PRESS, MENU, SECTIONS, SiteListing
from app.security import issue_session, verify_session
from app.services.briefing import strip_dropped_sections
from app.services.crawler import (
    DuplicateDetector,
    SearchSpec,
    _google_article_tokens,
    SiteListingCollector,
    decode_html,
    is_google_news,
    listing_article_urls,
    match_keywords,
    page_metadata,
)
from app.services.images import _normalize, body_image_urls
from app.services.job_runner import JobRunner, is_collection_day, report_window
from app.services.publisher import PublishError
from app.services.site_builder import build_site


def make_settings(
    tmp_path: Path,
    *,
    auto_register: bool = False,
    auto_publish: bool = False,
    purge_previous_dates: bool = False,
) -> Settings:
    return Settings(
        host="127.0.0.1",
        port=8000,
        timezone=ZoneInfo("Asia/Seoul"),
        data_dir=tmp_path / "storage",
        schedule_enabled=False,
        auto_register=auto_register,
        auto_publish=auto_publish,
        purge_previous_dates=purge_previous_dates,
        admin_api_key="test-key",
        admin_session_hours=1,
        origin_shared_secret="",
        ollama_url="http://127.0.0.1:11434",
        ollama_model="test-model",
        ollama_timeout_seconds=1,
        rss_enabled=False,
        images_enabled=True,
        request_timeout_seconds=1,
        user_agent="test",
    )


def make_job(database: Database, **overrides) -> dict:
    payload = {
        "report_date": "2026-08-25",
        "section": "council",
        "name": "군산시의회",
        "keywords": ["군산시의회"],
        "exclude_keywords": [],
        "sites": [],
        "preferred_sites": [],
        "match_mode": "any",
        "generate_briefing": False,
        "window_start": "2026-08-24T09:00:00+09:00",
        "window_end": "2026-08-25T05:00:00+09:00",
    }
    payload.update(overrides)
    return database.create_job(**payload)


def sample_article(
    job_id: str,
    article_id: str,
    url: str,
    publisher: str,
    content: str,
    *,
    title: str = "군산시의회, 지역 현안 논의",
    content_hash: str = "same-content",
) -> dict:
    return {
        "id": article_id,
        "job_id": job_id,
        "report_date": "2026-08-25",
        "title": title,
        "publisher": publisher,
        "source_url": url,
        "published_at": "2026-08-24T11:00:00+09:00",
        "scraped_at": "2026-08-24T12:00:00+09:00",
        "summary": content[:40],
        "content": content,
        "content_hash": content_hash,
        "matched_keywords": ["군산시의회"],
        "preferred": 0,
        "duplicate_of": None,
    }


def test_window_is_previous_day_nine_to_current_day_five():
    # 2026-08-25는 화요일. 평일은 전날 09:00부터다.
    start, end = report_window(date(2026, 8, 25), ZoneInfo("Asia/Seoul"))
    assert start.isoformat() == "2026-08-24T09:00:00+09:00"
    assert end.isoformat() == "2026-08-25T05:00:00+09:00"


def test_monday_window_reaches_back_to_friday():
    seoul = ZoneInfo("Asia/Seoul")
    monday = date(2026, 8, 31)
    assert monday.weekday() == 0
    start, end = report_window(monday, seoul)
    # 금요일 09:00부터 월요일 05:00까지 = 금·토·일 사흘치
    assert start.isoformat() == "2026-08-28T09:00:00+09:00"
    assert end.isoformat() == "2026-08-31T05:00:00+09:00"
    assert (end - start).days == 2


def test_weekends_are_not_collection_days():
    assert is_collection_day(date(2026, 8, 28)) is True   # 금
    assert is_collection_day(date(2026, 8, 29)) is False  # 토
    assert is_collection_day(date(2026, 8, 30)) is False  # 일
    assert is_collection_day(date(2026, 8, 31)) is True   # 월


def test_weekdays_register_themselves(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    runner = JobRunner(database, settings)

    monday = date(2026, 8, 31)
    created = runner.ensure_registered(monday)
    assert set(created) == set(SECTIONS)

    jobs = {job["section"]: job for job in database.jobs(report_date="2026-08-31")}
    # 갈래 기본 조건이 그대로 들어간다.
    assert jobs["cityhall"]["exclude_keywords"] == ["군산시의회", "군산시의원"]
    assert jobs["broadcast"]["sites"] == list(SECTIONS["broadcast"].sites)
    assert jobs["council"]["preferred_sites"] == list(LOCAL_PRESS)
    # 월요일이므로 금요일 09:00부터다.
    assert jobs["council"]["window_start"] == "2026-08-28T09:00:00+09:00"

    # 이미 등록된 날짜는 건드리지 않는다.
    assert runner.ensure_registered(monday) == []

    # 주말은 등록하지 않는다.
    assert runner.ensure_registered(date(2026, 8, 29)) == []
    assert runner.ensure_registered(date(2026, 8, 30)) == []
    assert database.jobs(report_date="2026-08-29") == []


def test_admin_edits_survive_auto_registration(tmp_path: Path):
    """관리자가 고쳐 둔 조건은 자동 등록이 덮어쓰지 않는다."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    runner = JobRunner(database, settings)

    make_job(database, report_date="2026-09-01", section="council", keywords=["군산시의회", "예산"])
    created = runner.ensure_registered(date(2026, 9, 1))

    assert "council" not in created
    council = database.job_for_section("2026-09-01", "council")
    assert council["keywords"] == ["군산시의회", "예산"]


def test_server_registers_todays_collection_on_startup(tmp_path: Path):
    """자동 등록이 켜져 있으면 관리자가 손대지 않아도 오늘 몫이 등록된다."""
    settings = make_settings(tmp_path, auto_register=True)
    with TestClient(create_app(settings)):
        pass

    database = Database(settings.database_path)
    today = datetime.now(settings.timezone).date()
    registered = {job["section"] for job in database.jobs(report_date=today.isoformat())}
    if is_collection_day(today):
        assert registered == set(SECTIONS)
    else:
        assert registered == set()  # 주말에는 등록하지 않는다


def test_weekend_jobs_are_refused(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        for weekend in ("2026-08-29", "2026-08-30"):
            single = client.post(
                "/api/admin/jobs", json={"report_date": weekend, "section": "council"}
            )
            assert single.status_code == 400
            assert "토요일" in single.json()["detail"]
            assert client.post("/api/admin/jobs/bulk", json={"report_date": weekend}).status_code == 400

        assert client.post(
            "/api/admin/jobs/bulk", json={"report_date": "2026-08-31"}
        ).status_code == 201


def test_broadcast_section_is_restricted_to_the_stations():
    section = SECTIONS["broadcast"]
    assert [tab.label for tab in MENU] == [
        "군산시의회 AI 브리핑",
        "군산시의회",
        "군산시청 AI 브리핑",
        "군산시청",
        "방송소식",
        "타의회 보도자료",
    ]
    assert set(section.sites) == {"news.kbs.co.kr", "jmbc.co.kr", "jtv.co.kr", "kcn.tv"}

    spec = SearchSpec(keywords=("군산",), sites=section.sites)
    assert spec.query.startswith("(site:news.kbs.co.kr OR ")
    assert '"군산"' in spec.query

    assert spec.matches_site("https://news.kbs.co.kr/news/view.do?ncd=1")
    assert spec.matches_site("https://www.jmbc.co.kr/news/view/1")
    assert spec.matches_site("http://kcn.tv/news/1")
    # 다른 매체가 같은 내용을 전재해도 방송소식에는 들어오지 않는다.
    assert not spec.matches_site("https://www.jbcj.kr/news/articleView.html?idxno=1")
    assert not spec.matches_site("https://news.kbs.co.kr.evil.example/news/1")
    # 매체를 지정하지 않은 갈래는 모두 통과한다.
    assert SearchSpec(keywords=("군산시의회",)).matches_site("https://any.example/1")


def test_local_press_is_searched_separately_and_listed_first(tmp_path: Path):
    for key in ("council", "cityhall"):
        assert SECTIONS[key].preferred_sites == LOCAL_PRESS
        # 우선 매체는 걸러내는 조건이 아니다. 다른 매체 기사도 함께 담는다.
        assert SECTIONS[key].sites == ()
    assert "todaygunsan.co.kr" in LOCAL_PRESS and "jjan.kr" in LOCAL_PRESS

    spec = SearchSpec(keywords=("군산시의회",), preferred_sites=LOCAL_PRESS)
    queries = spec.queries
    assert len(queries) == 2
    assert queries[0].startswith("(site:") and '"군산시의회"' in queries[0]
    assert queries[1] == '"군산시의회"'

    assert spec.is_preferred("https://www.todaygunsan.co.kr/news/1")
    assert not spec.is_preferred("https://www.chosun.com/news/1")
    assert not spec.is_preferred("https://todaygunsan.co.kr.evil.example/1")

    # 목록에서 지역 매체가 앞에 온다.
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database, preferred_sites=list(LOCAL_PRESS))
    outside = sample_article(job["id"], "outside", "https://a.example/1", "서울신문", DISTINCT_BODIES[0])
    local = sample_article(job["id"], "local", "https://www.todaygunsan.co.kr/1", "투데이 군산", DISTINCT_BODIES[1])
    local["preferred"] = 1
    local["content_hash"] = "hash-local"
    database.upsert_article(outside)
    database.upsert_article(local)

    listed = database.articles(job["id"])
    assert [article["id"] for article in listed] == ["local", "outside"]
    assert listed[0]["preferred"] is True and listed[1]["preferred"] is False


def test_duplicates_keep_the_local_paper_version(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database, preferred_sites=list(LOCAL_PRESS))
    body = "군산시의회가 같은 보도자료를 배포해 여러 곳에 실렸다. " * 12
    # 외부 매체 판본이 사진이 더 많아도 지역 매체 판본을 대표로 남긴다.
    outside = sample_article(job["id"], "outside", "https://a.example/1", "서울신문", body)
    local = sample_article(job["id"], "local", "https://www.jjan.kr/1", "전북일보", body)
    local["preferred"] = 1
    database.upsert_article(outside)
    database.upsert_article(local)
    photo = tmp_path / "photo.jpg"
    photo.write_bytes(png_bytes(800, 600))
    database.replace_article_images(
        "outside",
        [{
            "id": "image-1", "article_id": "outside", "source_url": "https://a.example/p.jpg",
            "caption": "", "path": str(photo), "width": 800, "height": 600,
            "byte_size": 4321, "position": 0,
        }],
    )

    total, unique = DuplicateDetector().mark(database, job["id"])
    assert (total, unique) == (2, 1)
    assert database.articles(job["id"], unique_only=True)[0]["id"] == "local"


def test_search_spec_builds_query_and_filters_text():
    spec = SearchSpec(keywords=("군산시의회", "예산"), exclude_keywords=("전주시",), match_mode="any")
    assert spec.query == '("군산시의회" OR "예산") -"전주시"'

    assert match_keywords(spec, "군산시의회 임시회가 열렸다") == ["군산시의회"]
    assert match_keywords(spec, "전주시 군산시의회 공동 행사") is None
    assert match_keywords(spec, "군산시 축제 소식") is None

    strict = SearchSpec(keywords=("군산시의회", "예산"), match_mode="all")
    assert strict.query == '"군산시의회" "예산"'
    assert match_keywords(strict, "군산시의회 임시회") is None
    assert match_keywords(strict, "군산시의회 예산 심사") == ["군산시의회", "예산"]


def test_google_news_interstitial_tokens():
    assert is_google_news("https://news.google.com/rss/articles/CBMiXk")
    assert not is_google_news("https://www.jbcj.kr/news/articleView.html?idxno=77050")

    html = '<c-wiz data-n-a-id="CBMiXk" jscontroller="x" data-n-a-sg="Ae5Wzi" data-n-a-ts="1787810289">'
    assert _google_article_tokens(html) == ("CBMiXk", "Ae5Wzi", "1787810289")
    assert _google_article_tokens('<c-wiz data-n-a-id="CBMiXk">') is None


ARTICLE_HTML = """
<html><body>
  <div class="header"><img src="/img/logo.png"></div>
  <div id="ad_top"><img src="https://cdn.example.com/photo/sponsored-visual.jpg"></div>
  <article>
    <h1>군산시의회, 지역 현안 논의</h1>
    <p>군산시의회는 26일 임시회를 열고 조례안을 심의했다. 경제건설위원회는 소상공인 지원 조례
    개정안을 원안 가결했으며, 다음 달 2일 본회의에서 최종 의결할 예정이다. 의회는 이어서
    주차장 운영 조례안도 함께 처리했다고 밝혔다. 이번 임시회는 사흘간 진행된다.</p>
    <figure><img data-src="/news/photo/202608/77050_2930.jpg" src="data:image/gif;base64,R0lGOD"></figure>
    <div class="ad-banner"><img src="https://cdn.example.com/news/photo/house-ad.jpg"></div>
    <div class="sns-share"><img src="/news/photo/kakao_share_button.png"></div>
  </article>
</body></html>
"""


# 중복 판정에 걸리지 않도록 서로 다른 본문을 쓴다.
DISTINCT_BODIES = [
    "군산시의회 경제건설위원회가 소상공인 지원 조례 개정안을 원안 가결했다. " * 8,
    "군산시의회 행정복지위원회가 공론화 조례안을 심의해 통과시켰다고 밝혔다. " * 8,
    "군산시의회 의장이 구내식당을 찾아 조리원들의 노고를 격려하고 애로사항을 들었다. " * 8,
]


def png_bytes(width: int, height: int, color=(120, 140, 160)) -> bytes:
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, format="PNG")
    return buffer.getvalue()


def test_body_images_exclude_ads_and_chrome():
    urls = body_image_urls(ARTICLE_HTML, "https://news.example.com/article/1")

    assert urls == ["https://news.example.com/news/photo/202608/77050_2930.jpg"]
    joined = " ".join(urls)
    for unwanted in ("logo", "sponsored", "house-ad", "share_button"):
        assert unwanted not in joined


def test_body_images_skip_known_ad_networks():
    html = '<article><p>기사 본문</p><img src="https://pagead2.googlesyndication.com/photo/x.jpg"></article>'
    assert body_image_urls(html, "https://news.example.com/a") == []


def test_only_real_photos_survive_normalization():
    assert _normalize(png_bytes(64, 64)) is None, "아이콘 크기는 제외"
    assert _normalize(png_bytes(970, 90)) is None, "가로로 긴 배너는 제외"
    assert _normalize(b"not-an-image") is None

    normalized = _normalize(png_bytes(800, 600))
    assert normalized is not None
    data, width, height = normalized
    assert (width, height) == (800, 600)
    assert data.startswith(b"\xff\xd8"), "JPEG로 통일해 저장"

    oversized = _normalize(png_bytes(2400, 1200))
    assert oversized is not None and oversized[1] == 1600


def test_images_are_stored_and_removed_with_the_job(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database)
    database.upsert_article(sample_article(job["id"], "one", "https://a.example/1", "가신문", "본문"))

    photo = tmp_path / "photo.jpg"
    photo.write_bytes(png_bytes(800, 600))
    database.replace_article_images(
        "one",
        [
            {
                "id": "image-1", "article_id": "one", "source_url": "https://a.example/p.jpg",
                "caption": "본회의장", "path": str(photo), "width": 800, "height": 600,
                "byte_size": 4321, "position": 0,
            }
        ],
    )

    stored = database.articles(job["id"])[0]["images"]
    assert [image["id"] for image in stored] == ["image-1"]
    assert database.get_image("image-1")["job_id"] == job["id"]

    # Re-running a job replaces the previous image set rather than piling up.
    database.replace_article_images("one", [])
    assert database.articles(job["id"])[0]["images"] == []
    assert database.get_image("image-1") is None


def test_dedupe_keeps_the_copy_with_more_photos(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database)
    body = "같은 보도자료가 여러 언론사에 실렸습니다. " * 12
    database.upsert_article(sample_article(job["id"], "plain", "https://a.example/1", "가신문", body))
    database.upsert_article(sample_article(job["id"], "rich", "https://b.example/2", "나신문", body))

    photo = tmp_path / "photo.jpg"
    photo.write_bytes(png_bytes(800, 600))
    database.replace_article_images(
        "rich",
        [
            {
                "id": f"image-{index}", "article_id": "rich", "source_url": f"https://b.example/{index}.jpg",
                "caption": "", "path": str(photo), "width": 800, "height": 600,
                "byte_size": 4321, "position": index,
            }
            for index in range(2)
        ],
    )

    total, unique = DuplicateDetector().mark(database, job["id"])
    assert (total, unique) == (2, 1)
    survivor = database.articles(job["id"], unique_only=True)[0]
    assert survivor["id"] == "rich"
    assert len(survivor["images"]) == 2


def test_duplicate_detection(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database)
    database.upsert_article(sample_article(job["id"], "one", "https://a.example/1", "가신문", "동일한 보도자료 본문입니다."))
    database.upsert_article(sample_article(job["id"], "two", "https://b.example/2", "나신문", "동일한 보도자료 본문입니다."))

    total, unique = DuplicateDetector().mark(database, job["id"])
    assert (total, unique) == (2, 1)
    all_articles = database.articles(job["id"])
    assert sum(article["duplicate_of"] is not None for article in all_articles) == 1
    assert len(database.articles(job["id"], unique_only=True)) == 1

    photo = tmp_path / "photo.jpg"
    photo.write_bytes(png_bytes(900, 600))
    database.replace_article_images(
        "one",
        [
            {
                "id": "image-1", "article_id": "one", "source_url": "https://a.example/p.jpg",
                "caption": "", "path": str(photo), "width": 900, "height": 600,
                "byte_size": 5000, "position": 0,
            }
        ],
    )
    assert len(database.articles(job["id"])[0]["images"]) == 1


def test_due_jobs_only_after_the_window_closes(tmp_path: Path):
    database = Database(make_settings(tmp_path).database_path)
    database.initialize()
    job = make_job(database)
    assert database.due_jobs("2026-08-25T04:59:00+09:00") == []
    assert [item["id"] for item in database.due_jobs("2026-08-25T05:00:00+09:00")] == [job["id"]]

    database.update_job_run(job["id"], status="complete", article_count=3, unique_count=2)
    assert database.due_jobs("2026-08-26T05:00:00+09:00") == []


def test_interrupted_run_returns_to_pending(tmp_path: Path):
    database = Database(make_settings(tmp_path).database_path)
    database.initialize()
    job = make_job(database)
    database.update_job_run(job["id"], status="running")
    assert database.due_jobs("2026-08-25T05:00:00+09:00") == []

    assert database.reset_interrupted_jobs() == 1
    reset = database.get_job(job["id"])
    assert reset["status"] == "pending"
    assert [item["id"] for item in database.due_jobs("2026-08-25T05:00:00+09:00")] == [job["id"]]


def test_admin_session_token_round_trip():
    token, max_age = issue_session("test-key", 1)
    assert max_age == 3600
    assert verify_session("test-key", token)
    assert not verify_session("other-key", token)
    assert not verify_session("test-key", "9999999999.deadbeef")


def test_public_pages_are_read_only(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        assert client.get("/health").json()["status"] == "ok"
        assert client.get("/").status_code == 200
        assert client.get("/admin").status_code == 200
        assert client.get("/api/dates").json() == {"dates": []}
        assert client.get("/api/jobs").json() == {"jobs": []}
        assert client.post("/api/admin/jobs", json={"report_date": "2026-08-25", "section": "council"}).status_code == 401
        assert client.post("/api/admin/login", json={"key": "wrong"}).status_code == 401


def test_admin_can_register_and_delete_a_job(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        assert client.post("/api/admin/login", json={"key": "test-key"}).status_code == 200

        created = client.post(
            "/api/admin/jobs",
            json={
                "report_date": "2026-08-25",
                "section": "council",
                "keywords": ["군산시의회", " 군산시의회 "],
                "exclude_keywords": ["전주시"],
                "match_mode": "any",
                "generate_briefing": False,
            },
        )
        assert created.status_code == 201
        job = created.json()["job"]
        assert job["keywords"] == ["군산시의회"]
        assert job["status"] == "pending"
        assert job["window_start"] == "2026-08-24T09:00:00+09:00"
        assert job["window_end"] == "2026-08-25T05:00:00+09:00"

        assert client.get("/api/dates").json() == {"dates": ["2026-08-25"]}
        assert len(client.get("/api/jobs?report_date=2026-08-25").json()["jobs"]) == 1

        duplicate = client.post(
            "/api/admin/jobs", json={"report_date": "2026-08-25", "section": "council"}
        )
        assert duplicate.status_code == 409

        assert client.post("/api/admin/jobs", json={"report_date": "2026-08-25"}).status_code == 422
        assert client.post(
            "/api/admin/jobs", json={"report_date": "2026-08-25", "section": "nope"}
        ).status_code == 422
        overlap = client.post(
            "/api/admin/jobs",
            json={
                "report_date": "2026-08-26",
                "section": "cityhall",
                "keywords": ["군산시"],
                "exclude_keywords": ["군산시"],
            },
        )
        assert overlap.status_code == 400

        assert client.delete(f"/api/admin/jobs/{job['id']}").status_code == 200
        assert client.get("/api/jobs").json() == {"jobs": []}


def test_menu_pairs_briefing_tabs_with_their_sections():
    assert [tab.view for tab in MENU] == [
        "briefing", "articles", "briefing", "articles", "articles", "articles",
    ]
    assert set(SECTIONS) == {"council", "cityhall", "broadcast", "other_councils"}
    assert SECTIONS["cityhall"].exclude_keywords == ("군산시의회", "군산시의원")
    assert SECTIONS["other_councils"].has_briefing is False
    # 전북특별자치도의회와 도내 13개 시·군의회(군산시의회 제외).
    assert len(SECTIONS["other_councils"].keywords) == 14
    assert "전주시의회" in SECTIONS["other_councils"].keywords
    assert "군산시의회" not in SECTIONS["other_councils"].keywords


def test_section_registration_and_report_endpoints(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        assert len(client.get("/api/menu").json()["menu"]) == len(MENU)
        client.post("/api/admin/login", json={"key": "test-key"})

        bulk = client.post("/api/admin/jobs/bulk", json={"report_date": "2026-08-25"})
        assert bulk.status_code == 201
        created = {job["section"] for job in bulk.json()["created"]}
        assert created == set(SECTIONS)

        # 갈래별 기본 검색 조건이 그대로 적용된다.
        jobs = {job["section"]: job for job in client.get("/api/admin/jobs").json()["jobs"]}
        assert jobs["cityhall"]["exclude_keywords"] == ["군산시의회", "군산시의원"]
        assert jobs["other_councils"]["generate_briefing"] is False
        assert jobs["council"]["generate_briefing"] is True

        # 같은 날짜의 같은 갈래는 다시 등록되지 않는다.
        again = client.post("/api/admin/jobs/bulk", json={"report_date": "2026-08-25"})
        assert again.json()["created"] == []
        assert len(again.json()["skipped"]) == len(SECTIONS)

        overview = client.get("/api/reports/2026-08-25").json()
        assert set(overview["jobs"]) == set(SECTIONS)
        assert overview["jobs"]["council"]["status"] == "pending"

        # 승인 전에는 공개 화면에 아무것도 나가지 않는다.
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404
        assert client.get("/api/reports/2026-08-25/council/briefing").status_code == 404
        assert client.get("/api/reports/2026-08-25/nope/articles").status_code == 404
        assert client.get("/api/reports/2026-08-24/council/articles").status_code == 404


def test_confirmation_section_is_dropped_from_briefings():
    body = (
        "# 한눈에 보기\n조례안 5건이 가결됐다.\n\n"
        "# 주요 내용\n- 소상공인 지원 조례 원안 가결 [기사 3]\n\n"
        "# 확인 필요\n본회의 최종 의결 여부 확인 필요. [기사 3]"
    )
    cleaned = strip_dropped_sections(body)
    assert "확인 필요" not in cleaned
    assert "본회의 최종 의결" not in cleaned
    assert cleaned.endswith("- 소상공인 지원 조례 원안 가결 [기사 3]")

    # 모델이 다른 표현이나 다른 머리표를 써도 걷어낸다.
    assert "###" not in strip_dropped_sections("## 주요 내용\n- 항목\n\n### 확인이 필요한 점\n- 무엇")
    # 뒤에 다른 항목이 이어지면 그 항목은 남는다.
    kept = strip_dropped_sections("# 확인 필요\n- 무엇\n\n# 주요 내용\n- 항목")
    assert kept.startswith("# 주요 내용")


def test_public_pages_show_nothing_until_the_admin_approves(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "council", "generate_briefing": False},
        ).json()["job"]

        database = Database(make_settings(tmp_path).database_path)
        for index, publisher in enumerate(("가신문", "나신문", "다신문")):
            database.upsert_article(
                sample_article(
                    job["id"], f"a{index}", f"https://x.example/{index}", publisher,
                    DISTINCT_BODIES[index],
                    title=f"군산시의회 소식 {index}",
                    content_hash=f"hash-{index}",
                )
            )
        client.post(f"/api/admin/jobs/{job['id']}/run")

        # 수집만으로는 공개되지 않는다.
        overview = client.get("/api/reports/2026-08-25").json()["jobs"]["council"]
        assert overview["status"] == "complete"
        assert overview["approved"] is False
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404

        # 관리자 검토 화면에는 모든 기사가 보인다.
        review = client.get(f"/api/admin/jobs/{job['id']}/articles").json()
        assert len(review["articles"]) == 3
        assert review["job"]["needs_review"] is True

        # 한 건을 빼고 승인한다.
        approved = client.post(
            f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": ["a1"]}
        ).json()
        assert (approved["published_count"], approved["excluded_count"]) == (2, 1)

        published = client.get("/api/reports/2026-08-25/council/articles").json()["articles"]
        assert [article["id"] for article in published] == ["a0", "a2"]
        assert client.get("/api/reports/2026-08-25").json()["jobs"]["council"]["approved"] is True

        # 공개를 내리면 다시 비공개가 된다.
        client.post(f"/api/admin/jobs/{job['id']}/unapprove")
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404

        # 다시 수집하면 승인도 함께 풀린다.
        client.post(f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": []})
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 200
        client.post(f"/api/admin/jobs/{job['id']}/run")
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404


def test_only_the_council_section_waits_for_review(tmp_path: Path):
    assert SECTIONS["council"].requires_review is True
    assert SECTIONS["cityhall"].requires_review is False
    assert SECTIONS["other_councils"].requires_review is False

    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        client.post("/api/admin/jobs/bulk", json={"report_date": "2026-08-25"})
        jobs = {job["section"]: job for job in client.get("/api/admin/jobs").json()["jobs"]}
        for job in jobs.values():
            client.post(f"/api/admin/jobs/{job['id']}/run")

        overview = client.get("/api/reports/2026-08-25").json()["jobs"]
        # 군산시청·타의회는 수집 직후 바로 공개된다.
        assert overview["cityhall"]["approved"] is True
        assert overview["other_councils"]["approved"] is True
        assert client.get("/api/reports/2026-08-25/cityhall/articles").status_code == 200
        # 군산시의회만 검토를 기다린다.
        assert overview["council"]["approved"] is False
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404

        listed = {job["section"]: job for job in client.get("/api/admin/jobs").json()["jobs"]}
        assert listed["council"]["needs_review"] is True
        assert listed["cityhall"]["needs_review"] is False


def test_public_page_points_at_the_latest_collection_only(tmp_path: Path):
    """공개 화면은 최신 수집 일자 하나만 쓴다. 지난 날짜는 관리자 페이지에서 본다."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    make_job(database, report_date="2026-08-25", section="council")
    make_job(database, report_date="2026-08-27", section="council")

    with TestClient(create_app(settings)) as client:
        menu = client.get("/api/menu").json()
        assert menu["latest_date"] == "2026-08-27"
        assert menu["today"]

        # 지난 날짜도 주소를 직접 치면 열리지만, 화면은 최신분만 가리킨다.
        assert client.get("/api/dates").json()["dates"] == ["2026-08-27", "2026-08-25"]


def test_static_site_carries_only_approved_link_data(tmp_path: Path):
    """GitHub Pages에 올릴 정적본에는 승인된 링크 정보만 담긴다."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()

    published = make_job(
        database, report_date="2026-08-25", section="council", generate_briefing=True
    )
    waiting = make_job(database, report_date="2026-08-25", section="cityhall", name="군산시청")
    database.upsert_article(
        sample_article(
            published["id"], "a0", "https://www.jjan.kr/1", "전북일보", DISTINCT_BODIES[0],
            title="군산시의회 임시회 개회", content_hash="hash-0",
        )
    )
    database.upsert_article(
        sample_article(
            waiting["id"], "b0", "https://www.jjan.kr/2", "전북일보", DISTINCT_BODIES[1],
            title="군산시 청년 지원", content_hash="hash-1",
        )
    )
    database.save_briefing(published["id"], "# 한눈에 보기\n요약", "complete", "m", "2026-08-25T05:10:00Z")
    database.set_approved(published["id"], True)  # 군산시청은 승인하지 않은 채로 둔다

    site = tmp_path / "site"
    result = build_site(database, settings, site)

    assert result["dates"] == ["2026-08-25"]
    assert (site / "index.html").is_file()
    assert (site / "styles.css").is_file()
    assert (site / "app.js").is_file()
    assert (site / ".nojekyll").is_file()

    index = json.loads((site / "data" / "index.json").read_text(encoding="utf-8"))
    assert index["latest_date"] == "2026-08-25"
    assert len(index["menu"]) == len(MENU)

    payload = json.loads((site / "data" / "2026-08-25.json").read_text(encoding="utf-8"))
    # 승인한 갈래만 실린다.
    assert set(payload["sections"]) == {"council"}
    article = payload["sections"]["council"]["articles"][0]
    assert set(article) == {
        "id", "title", "publisher", "published_at", "source_url", "matched_keywords", "preferred",
    }
    assert "content" not in article
    assert payload["sections"]["council"]["briefing"]["body"].startswith("# 한눈에 보기")

    # 정적 화면은 API 대신 JSON을 읽고, 관리자 링크는 싣지 않는다.
    html = (site / "index.html").read_text(encoding="utf-8")
    assert 'data-mode="static"' in html
    assert './styles.css' in html and './app.js' in html
    assert "/admin" not in html and "{{ASSET_VERSION}}" not in html


def test_public_articles_carry_only_link_information(tmp_path: Path):
    """공개 화면에는 제목·언론사·발행시각·원문 주소만 나간다(직접링크 방식)."""
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "cityhall", "generate_briefing": False},
        ).json()["job"]

        database = Database(make_settings(tmp_path).database_path)
        database.upsert_article(
            sample_article(
                job["id"], "a0", "https://www.jjan.kr/article/1", "전북일보", DISTINCT_BODIES[0],
                title="군산시, 청년 지원 사업 확대", content_hash="hash-0",
            )
        )
        client.post(f"/api/admin/jobs/{job['id']}/run")

        article = client.get("/api/reports/2026-08-25/cityhall/articles").json()["articles"][0]
        assert set(article) == {
            "id", "title", "publisher", "published_at", "source_url", "matched_keywords", "preferred",
        }
        # 본문과 사진은 공개 응답에 실리지 않는다.
        assert "content" not in article and "summary" not in article and "images" not in article
        # 링크는 언론사 기사 주소를 그대로 가리킨다(단순링크가 아닌 직접링크).
        assert article["source_url"] == "https://www.jjan.kr/article/1"
        assert article["publisher"] == "전북일보"

        # 관리자 검토 화면에서는 본문을 그대로 볼 수 있다.
        review = client.get(f"/api/admin/jobs/{job['id']}/articles").json()["articles"][0]
        assert review["content"].startswith("군산시의회 경제건설위원회")


def test_admin_defined_order_drives_the_public_list(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "council", "generate_briefing": False},
        ).json()["job"]

        database = Database(make_settings(tmp_path).database_path)
        for index in range(3):
            database.upsert_article(
                sample_article(
                    job["id"], f"a{index}", f"https://x.example/{index}", f"{index}신문",
                    DISTINCT_BODIES[index],
                    title=f"군산시의회 소식 {index}", content_hash=f"hash-{index}",
                )
            )
        client.post(f"/api/admin/jobs/{job['id']}/run")

        # 관리자가 고른 차례대로 승인한다.
        approved = client.post(
            f"/api/admin/jobs/{job['id']}/approve",
            json={"excluded_ids": [], "ordered_ids": ["a2", "a0", "a1"]},
        ).json()
        assert approved["ordered_count"] == 3

        published = client.get("/api/reports/2026-08-25/council/articles").json()["articles"]
        assert [article["id"] for article in published] == ["a2", "a0", "a1"]

        # 검토 화면도 같은 차례로 다시 열린다.
        review = client.get(f"/api/admin/jobs/{job['id']}/articles").json()["articles"]
        assert [article["id"] for article in review] == ["a2", "a0", "a1"]
        assert [article["sort_order"] for article in review] == [1, 2, 3]

        # 순서를 비워 승인하면 기본 차례(지역 매체 → 최신순)로 돌아간다.
        client.post(
            f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": [], "ordered_ids": []}
        )
        reset = client.get(f"/api/admin/jobs/{job['id']}/articles").json()["articles"]
        assert {article["sort_order"] for article in reset} == {0}


def test_photos_are_not_collected_by_default(monkeypatch):
    monkeypatch.delenv("IMAGES_ENABLED", raising=False)
    from app.config import load_settings

    assert load_settings().images_enabled is False


def test_excluded_articles_stay_out_of_the_briefing(tmp_path: Path):
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database, generate_briefing=True)
    for index in range(3):
        database.upsert_article(
            sample_article(
                job["id"], f"a{index}", f"https://x.example/{index}", f"{index}신문",
                DISTINCT_BODIES[index],
                title=f"군산시의회 소식 {index}",
                content_hash=f"hash-{index}",
            )
        )
    database.update_job_run(job["id"], status="complete", article_count=3, unique_count=3)

    seen: dict[str, list] = {}

    async def fake_create(job_row, articles):
        seen["articles"] = articles
        return {
            "body": "# 한눈에 보기\n요약", "status": "complete",
            "model": "test-model", "generated_at": "2026-08-25T05:10:00Z",
        }

    runner = JobRunner(database, settings)
    runner.briefing_service.create = fake_create
    result = asyncio.run(runner.approve(job["id"], ["a1"]))

    assert result["published_count"] == 2
    assert [article["id"] for article in seen["articles"]] == ["a0", "a2"]
    assert database.get_job(job["id"])["approved_at"]


def test_manual_run_without_rss_completes_empty(tmp_path: Path):
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "council", "generate_briefing": False},
        ).json()["job"]

        result = client.post(f"/api/admin/jobs/{job['id']}/run").json()
        assert result["status"] == "complete"
        assert (result["article_count"], result["unique_count"]) == (0, 0)

        assert client.get(f"/api/jobs/{job['id']}").json()["job"]["status"] == "complete"


LINKED_HTML = """
<html><head>
  <title>군산시의회, 예산 심사 마무리 - 군산신문</title>
  <meta property="og:title" content="군산시의회, 2026년도 예산 심사 마무리" />
  <meta property="article:published_time" content="2026-08-24T15:30:00+09:00" />
  <meta property="og:site_name" content="군산신문" />
</head><body><article>
  <p>군산시의회는 24일 제260회 임시회를 열고 2026년도 추가경정예산안 심사를 마무리했다.</p>
  <p>예산결산특별위원회는 사업의 시급성과 집행 가능성을 따져 일부 항목을 조정했다고 밝혔다.</p>
  <p>의회는 남은 회기 동안 조례안 심사를 이어 갈 예정이다.</p>
</article></body></html>
"""


def test_page_metadata_reads_title_publisher_and_time():
    seoul = ZoneInfo("Asia/Seoul")
    meta = page_metadata(LINKED_HTML, "https://www.gunsannews.com/news/1", seoul)
    assert meta["title"] == "군산시의회, 2026년도 예산 심사 마무리"
    assert meta["publisher"] == "군산신문"
    assert meta["published_at"].isoformat() == "2026-08-24T15:30:00+09:00"

    # 메타 태그가 없으면 <title>에서 매체 이름을 떼고, <time>에서 시각을 읽는다.
    bare = """<html><head><title>군산시의회 임시회 개회와 주요 안건 처리 - 어떤신문</title></head>
    <body><time datetime="2026.08.24 09:15">2026.08.24</time></body></html>"""
    fallback = page_metadata(bare, "https://unknown.example/news/9", seoul)
    assert fallback["title"] == "군산시의회 임시회 개회와 주요 안건 처리"
    # 제목 끝에 붙은 매체 이름을 매체로 쓴다.
    assert fallback["publisher"] == "어떤신문"
    assert fallback["published_at"].isoformat() == "2026-08-24T09:15:00+09:00"

    # 매체 이름을 어디에도 적지 않으면 www를 뗀 주소를 쓰고,
    # 시각은 지면에 찍힌 "입력 …" 표기에서 읽는다.
    plain = """<html><head><title>군산시의회 소식</title></head>
    <body><h1>군산시의회, 조례안 3건 의결</h1>
    <span>입력 2026.08.24 16:05</span></body></html>"""
    guessed = page_metadata(plain, "https://www.nowhere.example/read/3", seoul)
    assert guessed["publisher"] == "nowhere.example"
    assert guessed["published_at"].isoformat() == "2026-08-24T16:05:00+09:00"


def _linked_article_client(monkeypatch, html: str = LINKED_HTML):
    """관리자가 붙여 넣은 주소를 열면 위 기사 페이지가 나오도록 만든다."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, html=html)

    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    monkeypatch.setattr("app.services.job_runner.httpx.AsyncClient", factory)


def test_admin_can_add_an_article_by_pasting_its_link(tmp_path: Path, monkeypatch):
    """검토 화면에서 붙여 넣은 주소가 그대로 스크랩에 더해진다."""
    _linked_article_client(monkeypatch)
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "council", "generate_briefing": False},
        ).json()["job"]
        client.post(f"/api/admin/jobs/{job['id']}/run")

        response = client.post(
            f"/api/admin/jobs/{job['id']}/articles",
            json={"url": "https://www.gunsannews.com/news/articleView.html?idxno=1"},
        )
        assert response.status_code == 201
        added = response.json()["article"]
        assert added["title"] == "군산시의회, 2026년도 예산 심사 마무리"
        assert added["publisher"] == "군산신문"
        assert added["published_at"] == "2026-08-24T15:30:00+09:00"
        assert added["manual"] is True and added["duplicate_of"] is None
        assert response.json()["already_present"] is False

        # 검토 목록에 바로 보이고, 승인하면 공개 화면에 오른다.
        review = client.get(f"/api/admin/jobs/{job['id']}/articles").json()["articles"]
        assert [item["id"] for item in review] == [added["id"]]
        client.post(f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": []})
        public = client.get("/api/reports/2026-08-25/council/articles").json()["articles"]
        assert public[0]["source_url"] == "https://www.gunsannews.com/news/articleView.html?idxno=1"
        assert public[0]["title"] == "군산시의회, 2026년도 예산 심사 마무리"

        # 같은 주소를 다시 넣어도 기사는 하나로 유지된다.
        again = client.post(
            f"/api/admin/jobs/{job['id']}/articles",
            json={"url": "https://www.gunsannews.com/news/articleView.html?idxno=1"},
        ).json()
        assert again["already_present"] is True
        assert len(client.get(f"/api/admin/jobs/{job['id']}/articles").json()["articles"]) == 1

        # 기사 주소가 아니면 이유를 알려 주고 아무것도 넣지 않는다.
        bad = client.post(f"/api/admin/jobs/{job['id']}/articles", json={"url": "gunsan.go.kr"})
        assert bad.status_code == 400 and "http" in bad.json()["detail"]


def test_manually_added_article_survives_duplicate_grouping(tmp_path: Path):
    """관리자가 직접 넣은 기사는 같은 내용이 있어도 묶여 사라지지 않는다."""
    settings = make_settings(tmp_path)
    database = Database(settings.database_path)
    database.initialize()
    job = make_job(database)

    database.upsert_article(
        sample_article(job["id"], "auto", "https://a.example/1", "어떤신문", DISTINCT_BODIES[0])
    )
    manual = sample_article(job["id"], "manual", "https://b.example/1", "군산신문", DISTINCT_BODIES[0])
    manual["manual"] = 1
    database.upsert_article(manual)

    total, unique = DuplicateDetector().mark(database, job["id"])
    assert (total, unique) == (2, 1)
    kept = [article for article in database.articles(job["id"]) if not article["duplicate_of"]]
    assert [article["id"] for article in kept] == ["manual"]
    assert kept[0]["manual"] is True


def test_bare_date_is_accepted_only_when_it_is_recent():
    """시각 표기가 없는 지면은 최근 날짜만 발행일로 인정한다."""
    seoul = ZoneInfo("Asia/Seoul")
    recent = (datetime.now(UTC).astimezone(seoul) - timedelta(days=2)).strftime("%Y.%m.%d")
    html = f"""<html><head><title>군산시의회 소식</title></head>
    <body><h1>군산시의회, 조례안 의결</h1><span>{recent}</span>
    <footer>2011.01.01 창간</footer></body></html>"""
    meta = page_metadata(html, "https://x.example/1", seoul)
    assert meta["published_at"].strftime("%Y.%m.%d") == recent

    # 오래된 날짜밖에 없으면 발행일을 지어내지 않는다(호출한 쪽이 현재 시각을 쓴다).
    stale = """<html><head><title>군산시의회 소식</title></head>
    <body><footer>2011.01.01 창간</footer></body></html>"""
    assert page_metadata(stale, "https://x.example/2", seoul)["published_at"] is None


def test_section_review_defaults_match_the_menu_definition(tmp_path: Path):
    """스위치를 손대기 전에는 갈래 기본값을 그대로 쓴다."""
    with TestClient(create_app(make_settings(tmp_path))) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        sections = {item["key"]: item for item in client.get("/api/admin/sections").json()["sections"]}
        assert sections["council"]["requires_review"] is True
        assert sections["cityhall"]["requires_review"] is False
        assert sections["broadcast"]["requires_review"] is False
        assert sections["other_councils"]["requires_review"] is False
        # 기본값을 함께 알려 줘야 화면에서 "기본값과 다름"을 표시할 수 있다.
        assert all(
            item["requires_review"] == item["default_requires_review"] for item in sections.values()
        )


def test_switching_a_menu_to_auto_approval_publishes_without_review(tmp_path: Path):
    """군산시의회 자동 승인을 켜면 수집한 결과가 검토 없이 공개된다."""
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-25", "section": "council", "generate_briefing": False},
        ).json()["job"]

        # 기본 상태에서는 수집만 하고 승인을 기다린다.
        assert client.post(f"/api/admin/jobs/{job['id']}/run").json()["approved"] is False
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404

        switched = client.post("/api/admin/sections/council", json={"requires_review": False})
        assert switched.status_code == 200
        assert switched.json()["section"]["requires_review"] is False
        assert switched.json()["section"]["default_requires_review"] is True

        # 다음 수집부터 곧바로 공개된다.
        database = Database(settings.database_path)
        database.upsert_article(
            sample_article(
                job["id"], "b0", "https://www.jjan.kr/article/7", "전북일보", DISTINCT_BODIES[0],
                content_hash="hash-switch",
            )
        )
        assert client.post(f"/api/admin/jobs/{job['id']}/run").json()["approved"] is True
        published = client.get("/api/reports/2026-08-25/council/articles").json()["articles"]
        assert [article["id"] for article in published] == ["b0"]

        # 다시 끄면 승인을 기다리는 상태로 돌아간다.
        client.post("/api/admin/sections/council", json={"requires_review": True})
        assert client.post(f"/api/admin/jobs/{job['id']}/run").json()["approved"] is False
        assert client.get("/api/reports/2026-08-25/council/articles").status_code == 404

        # 설정은 저장되어 다음 실행에도 남는다.
        assert Database(settings.database_path).section_review_flags() == {"council": True}
        assert client.post("/api/admin/sections/nowhere", json={"requires_review": True}).status_code == 404


def test_public_menu_reports_the_current_publishing_mode(tmp_path: Path):
    """공개 화면과 정적 사이트도 지금 적용 중인 공개 방식을 그대로 싣는다."""
    settings = make_settings(tmp_path)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        client.post("/api/admin/sections/council", json={"requires_review": False})
        menu = client.get("/api/menu").json()
        assert menu["sections"]["council"]["requires_review"] is False

    site = tmp_path / "site"
    build_site(Database(settings.database_path), settings, site)
    index = json.loads((site / "data" / "index.json").read_text(encoding="utf-8"))
    assert index["sections"]["council"]["requires_review"] is False


def _stub_publisher(monkeypatch, error: Exception | None = None):
    """게시 함수를 가짜로 바꾼다. 시험에서 git을 건드리지 않기 위해서다."""
    calls: list[str] = []

    def fake_publish(database, settings, **kwargs):
        calls.append(datetime.now(UTC).isoformat())
        if error:
            raise error
        return {
            "pushed": True,
            "pages_url": "https://example.github.io/brief/",
            "message": "",
            "built_at": "2026-08-25T05:10",
            "latest_date": "2026-08-25",
        }

    monkeypatch.setattr("app.services.job_runner.publish", fake_publish)
    return calls


def _council_job_with_one_article(client, settings, *, section: str = "council") -> dict:
    job = client.post(
        "/api/admin/jobs",
        json={"report_date": "2026-08-25", "section": section, "generate_briefing": False},
    ).json()["job"]
    Database(settings.database_path).upsert_article(
        sample_article(
            job["id"], f"{section}-0", f"https://www.jjan.kr/{section}/1", "전북일보",
            DISTINCT_BODIES[0], content_hash=f"hash-{section}",
        )
    )
    return job


def test_manual_approval_publishes_the_site(tmp_path: Path, monkeypatch):
    """검토 뒤 승인하면 게시까지 이어서 끝낸다."""
    calls = _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=True)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = _council_job_with_one_article(client, settings)

        # 수집만으로는 게시하지 않는다. 군산시의회는 승인을 기다린다.
        assert client.post(f"/api/admin/jobs/{job['id']}/run").json()["approved"] is False
        assert calls == []

        result = client.post(
            f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": []}
        ).json()
        assert result["approved"] is True
        assert result["publish"] == {
            "status": "ok",
            "pushed": True,
            "pages_url": "https://example.github.io/brief/",
            "message": "",
        }
        assert len(calls) == 1


def test_auto_approved_menu_publishes_without_the_admin(tmp_path: Path, monkeypatch):
    """자동 승인 갈래는 수집·승인에 이어 게시까지 혼자 끝낸다."""
    calls = _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=True)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = _council_job_with_one_article(client, settings, section="cityhall")

        result = client.post(f"/api/admin/jobs/{job['id']}/run").json()
        assert result["approved"] is True
        assert result["publish"]["status"] == "ok"
        assert len(calls) == 1


def test_batch_run_publishes_once_for_every_menu(tmp_path: Path, monkeypatch):
    """밀린 수집을 한꺼번에 돌려도 게시는 마지막에 한 번만 한다."""
    calls = _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=True)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        created = client.post(
            "/api/admin/jobs/bulk",
            json={"report_date": "2026-08-25", "sections": ["cityhall", "broadcast", "council"]},
        ).json()
        assert len(created["created"]) == 3

        payload = client.post("/api/admin/run-due").json()
        assert payload["job_count"] == 3
        # 자동 승인 두 갈래가 공개됐지만 게시는 한 번이다.
        assert len(calls) == 1
        assert payload["publish"]["status"] == "ok"


def test_failed_publishing_does_not_undo_the_approval(tmp_path: Path, monkeypatch):
    """게시가 실패해도 승인은 그대로 남고, 이유를 알려 준다."""
    _stub_publisher(monkeypatch, error=PublishError("원격 저장소에 접근할 수 없습니다."))
    settings = make_settings(tmp_path, auto_publish=True)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = _council_job_with_one_article(client, settings)
        client.post(f"/api/admin/jobs/{job['id']}/run")

        result = client.post(
            f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": []}
        ).json()
        assert result["approved"] is True
        assert result["publish"] == {
            "status": "failed",
            "message": "원격 저장소에 접근할 수 없습니다.",
        }
        # 승인은 저장돼 있으므로 공개 화면에는 이미 올라가 있다.
        published = client.get("/api/reports/2026-08-25/council/articles").json()["articles"]
        assert len(published) == 1


def test_auto_publish_can_be_turned_off(tmp_path: Path, monkeypatch):
    """AUTO_PUBLISH가 꺼져 있으면 승인만 하고 게시는 관리자가 직접 한다."""
    calls = _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=False)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        job = _council_job_with_one_article(client, settings)
        client.post(f"/api/admin/jobs/{job['id']}/run")
        result = client.post(
            f"/api/admin/jobs/{job['id']}/approve", json={"excluded_ids": []}
        ).json()
        assert result["publish"]["status"] == "skipped"
        assert calls == []


def test_todays_run_clears_the_earlier_dates(tmp_path: Path, monkeypatch):
    """오늘 수집이 끝나면 이전 날짜 스크랩은 기사·사진까지 지워진다."""
    _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=True, purge_previous_dates=True)
    database = Database(settings.database_path)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})

        old = _council_job_with_one_article(client, settings, section="cityhall")
        client.post(f"/api/admin/jobs/{old['id']}/run")
        stale_media = settings.media_dir / "2026-08-25"
        stale_media.mkdir(parents=True, exist_ok=True)
        (stale_media / "keep.jpg").write_bytes(b"x")
        assert database.dates() == ["2026-08-25"]

        # 다음 날짜(수요일) 수집이 끝나는 순간 앞선 날짜가 정리된다.
        fresh = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-26", "section": "cityhall", "generate_briefing": False},
        ).json()["job"]
        result = client.post(f"/api/admin/jobs/{fresh['id']}/run").json()

        assert result["purged_dates"] == ["2026-08-25"]
        assert database.dates() == ["2026-08-26"]
        assert database.articles(old["id"]) == []
        assert not stale_media.exists()

        # 관리자 화면 목록에도 오늘 몫만 남는다.
        listed = client.get("/api/admin/jobs").json()["jobs"]
        assert {job["report_date"] for job in listed} == {"2026-08-26"}


def test_previous_dates_are_kept_when_purging_is_off(tmp_path: Path, monkeypatch):
    """PURGE_PREVIOUS_DATES를 끄면 예전처럼 지난 날짜가 남는다."""
    _stub_publisher(monkeypatch)
    settings = make_settings(tmp_path, auto_publish=True, purge_previous_dates=False)
    with TestClient(create_app(settings)) as client:
        client.post("/api/admin/login", json={"key": "test-key"})
        old = _council_job_with_one_article(client, settings, section="cityhall")
        client.post(f"/api/admin/jobs/{old['id']}/run")
        fresh = client.post(
            "/api/admin/jobs",
            json={"report_date": "2026-08-26", "section": "cityhall", "generate_briefing": False},
        ).json()["job"]
        result = client.post(f"/api/admin/jobs/{fresh['id']}/run").json()

        assert "purged_dates" not in result
        assert Database(settings.database_path).dates() == ["2026-08-26", "2026-08-25"]


def test_euc_kr_pages_are_read_with_their_own_encoding():
    """지역 매체 상당수가 EUC-KR을 쓰고 charset을 <meta>에만 적어 둔다."""
    page = (
        "<html><head><title>군산뉴스</title>"
        '<meta http-equiv="content-type" content="text/html; charset=euc-kr"></head>'
        "<body><p>군산시의회는 2일 의원총회를 열었다.</p></body></html>"
    )
    response = httpx.Response(
        200, content=page.encode("cp949"), headers={"content-type": "text/html"}
    )
    # 헤더에 charset이 없으면 httpx는 UTF-8로 넘겨짚어 글자가 깨진다.
    assert "�" in response.text
    assert "군산시의회는 2일 의원총회를 열었다." in decode_html(response)

    # charset을 제대로 알려 주는 지면은 그대로 읽는다.
    utf8 = httpx.Response(
        200,
        content="<html><body>군산시청</body></html>".encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"},
    )
    assert "군산시청" in decode_html(utf8)


def test_site_wide_timestamp_is_not_mistaken_for_the_article_time():
    """지면 위쪽의 '최종업데이트' 시각이 아니라 기사에 찍힌 시각을 읽는다."""
    seoul = ZoneInfo("Asia/Seoul")
    now = datetime.now(UTC).astimezone(seoul)
    banner = now.strftime("%Y년 %m월 %d일(%a) %H:%M")
    written = (now - timedelta(days=1)).replace(hour=14, minute=13)
    html = f"""<html><head><title>서은식 시의원, “도심 개발 불균형 해소하자”</title></head>
    <body><span>최종업데이트 {banner}</span>
    <p>한정근 기자 / {written.strftime('%Y-%m-%d %H:%M:%S')}</p>
    <p>군산시의회가 도심 개발 불균형 해소를 촉구했다.</p></body></html>"""

    meta = page_metadata(html, "https://kmrnews.com:50000/ynews/ynews_view.php?pid=1", seoul)
    assert meta["publisher"] == "군산미래신문"
    assert meta["published_at"].strftime("%Y-%m-%d %H:%M") == written.strftime("%Y-%m-%d %H:%M")


def test_title_falls_back_when_the_page_title_is_only_the_paper_name():
    """<title>에 신문 이름만 있는 지면에서도 기사 제목을 찾아낸다."""
    seoul = ZoneInfo("Asia/Seoul")
    html = """<html><head><title>군산뉴스</title></head>
    <body><div id="article">
    <h1>군산시의회, 올해 공무국외연수 ‘안 간다’</h1>
    <p>군산시의회는 2일 의원총회를 열고 올해 의원 공무국외연수를 실시하지 않기로 뜻을 모았다.
    연수 예산은 반납해 민생 분야에 활용하는 방안도 함께 추진한다.</p>
    </div></body></html>"""
    meta = page_metadata(html, "https://www.newsgunsan.com/ngnews/ngNewsView.php?pid=1", seoul)
    assert meta["title"] == "군산시의회, 올해 공무국외연수 ‘안 간다’"
    assert meta["publisher"] == "군산뉴스"


LISTING_PAGE = """
<html><head><meta http-equiv="content-type" content="text/html; charset=euc-kr"></head>
<body>
  <a href="ngNewsView.php?code=NG2&pid=1&PHPSESSID=abc">군산시의회, 공무국외연수 취소</a>
  <a href="ngNewsView.php?code=NG2&pid=1&PHPSESSID=abc">사진</a>
  <a href="ngNewsView.php?code=NG2&pid=2&PHPSESSID=abc">군산 배추값 강세</a>
  <a href="ngNewsList.php?code=NG2">목록</a>
</body></html>
"""


def _listing_article(title: str, body: str, when) -> str:
    return f"""<html><head><title>군산뉴스</title>
    <meta http-equiv="content-type" content="text/html; charset=euc-kr"></head>
    <body><h1>{title}</h1><span>{when.strftime('%Y-%m-%d %H:%M')}</span>
    <p>{body}</p></body></html>"""


def test_local_papers_are_read_from_their_own_listing_pages(tmp_path: Path):
    """Google 뉴스가 색인하지 않는 지역지는 지면 목록을 직접 훑는다."""
    seoul = ZoneInfo("Asia/Seoul")
    start = datetime(2026, 9, 2, 9, 0, tzinfo=seoul)
    end = datetime(2026, 9, 3, 5, 0, tzinfo=seoul)
    inside = datetime(2026, 9, 2, 19, 20, tzinfo=seoul)
    outside = datetime(2026, 9, 1, 8, 0, tzinfo=seoul)

    pages = {
        "https://www.newsgunsan.com/ngnews/ngNewsList.php?code=NG2": LISTING_PAGE,
        "https://www.newsgunsan.com/ngnews/ngNewsView.php?code=NG2&pid=1": _listing_article(
            "군산시의회, 올해 공무국외연수 ‘안 간다’",
            "군산시의회는 2일 의원총회를 열고 공무국외연수를 하지 않기로 했다.",
            inside,
        ),
        # 키워드에 걸리지 않는 기사와 시간창 밖의 기사는 담지 않는다.
        "https://www.newsgunsan.com/ngnews/ngNewsView.php?code=NG2&pid=2": _listing_article(
            "군산 배추값 강세", "이달 들어 배추 도매가가 올랐다.", outside
        ),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        page = pages.get(str(request.url))
        if page is None:
            return httpx.Response(404)
        return httpx.Response(
            200, content=page.encode("cp949"), headers={"content-type": "text/html"}
        )

    settings = make_settings(tmp_path)
    settings = replace(settings, rss_enabled=True)
    collector = SiteListingCollector(settings)
    listing = SiteListing(
        "군산뉴스", "https://www.newsgunsan.com/ngnews/ngNewsList.php?code=NG2", "ngNewsView.php"
    )
    spec = SearchSpec(keywords=("군산시의회",), preferred_sites=LOCAL_PRESS)

    original = httpx.AsyncClient

    def factory(**kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return original(**kwargs)

    articles = asyncio.run(
        _with_client(factory, collector, (listing,), spec, start=start, end=end)
    )
    assert len(articles) == 1
    article = articles[0]
    assert article["title"] == "군산시의회, 올해 공무국외연수 ‘안 간다’"
    assert article["publisher"] == "군산뉴스"
    assert article["published_at"] == inside.isoformat()
    # 목록의 PHPSESSID는 떼고 저장한다. 같은 기사가 매번 새 주소로 쌓이면 안 된다.
    assert "PHPSESSID" not in article["source_url"]
    assert article["preferred"] == 1 and article["manual"] == 0


async def _with_client(factory, collector, listings, spec, *, start, end):
    import app.services.crawler as crawler

    original = crawler.httpx.AsyncClient
    crawler.httpx.AsyncClient = factory
    try:
        return await collector.collect(listings, spec, job_id="job-1", start=start, end=end)
    finally:
        crawler.httpx.AsyncClient = original


def test_listing_links_are_deduplicated_and_made_absolute():
    listing = SiteListing(
        "군산뉴스", "https://www.newsgunsan.com/ngnews/ngNewsList.php?code=NG2", "ngNewsView.php"
    )
    urls = listing_article_urls(
        LISTING_PAGE, listing, "https://www.newsgunsan.com/ngnews/ngNewsList.php?code=NG2"
    )
    assert urls == [
        "https://www.newsgunsan.com/ngnews/ngNewsView.php?code=NG2&pid=1",
        "https://www.newsgunsan.com/ngnews/ngNewsView.php?code=NG2&pid=2",
    ]
