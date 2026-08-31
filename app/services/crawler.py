from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx
import trafilatura
from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from app.config import Settings
from app.database import Database
from app.sections import publisher_for
from app.services.images import collect_article_images


MATCH_MODES = ("any", "all")


@dataclass(frozen=True)
class SearchSpec:
    """검색 조건. 관리자 페이지에서 고른 키워드가 그대로 들어온다."""

    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    # 지정하면 이 도메인에서 나온 기사만 남긴다(방송소식처럼 매체를 한정할 때).
    sites: tuple[str, ...] = ()
    # 따로 한 번 더 훑고 목록 위쪽에 둘 매체. 다른 매체 기사도 그대로 남는다.
    preferred_sites: tuple[str, ...] = ()
    match_mode: str = "any"

    @classmethod
    def from_job(cls, job: dict[str, Any]) -> "SearchSpec":
        return cls(
            keywords=tuple(job["keywords"]),
            exclude_keywords=tuple(job.get("exclude_keywords") or ()),
            sites=tuple(job.get("sites") or ()),
            preferred_sites=tuple(job.get("preferred_sites") or ()),
            match_mode=job.get("match_mode", "any"),
        )

    @staticmethod
    def _site_clause(sites: tuple[str, ...]) -> str:
        joined = " OR ".join(f"site:{site}" for site in sites)
        return joined if len(sites) == 1 else f"({joined})"

    @property
    def keyword_clause(self) -> str:
        quoted = [f'"{keyword}"' for keyword in self.keywords]
        if self.match_mode == "all":
            included = " ".join(quoted)
        else:
            included = quoted[0] if len(quoted) == 1 else "(" + " OR ".join(quoted) + ")"
        excluded = " ".join(f'-"{keyword}"' for keyword in self.exclude_keywords)
        return f"{included} {excluded}".strip()

    @property
    def queries(self) -> list[str]:
        """실제로 던질 검색식들.

        Google 뉴스는 한 검색에 100건까지만 돌려준다. 우선 매체를 지정한 갈래는
        그 매체만 좁혀 한 번 더 훑어야 지역지 기사가 상위 100건에 밀려 사라지지 않는다.
        """
        found = [self.query]
        if self.preferred_sites:
            found.insert(0, f"{self._site_clause(self.preferred_sites)} {self.keyword_clause}".strip())
        return found

    def is_preferred(self, url: str) -> bool:
        host = urlparse(url).netloc.lower().split(":")[0]
        return any(host == site or host.endswith(f".{site}") for site in self.preferred_sites)

    @property
    def query(self) -> str:
        sites = self._site_clause(self.sites) if self.sites else ""
        return " ".join(part for part in (sites, self.keyword_clause) if part).strip()

    def matches_site(self, url: str) -> bool:
        """매체를 한정한 갈래에서, 실제로 그 매체가 낸 기사인지 확인한다."""
        if not self.sites:
            return True
        host = urlparse(url).netloc.lower().split(":")[0]
        return any(host == site or host.endswith(f".{site}") for site in self.sites)


def normalize_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", unescape(value)).lower()
    value = re.sub(r"[^\w가-힣]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def match_keywords(spec: SearchSpec, text: str) -> list[str] | None:
    """조건을 만족하면 실제로 걸린 키워드를, 아니면 None을 돌려준다.

    RSS 색인은 검색어와 무관한 기사도 섞어 주므로, 원문 본문까지 받아 본 뒤
    여기서 한 번 더 걸러야 "해당되는 내용의 기사만" 남는다.
    """
    haystack = normalize_text(text)
    for keyword in spec.exclude_keywords:
        needle = normalize_text(keyword)
        if needle and needle in haystack:
            return None
    matched = [keyword for keyword in spec.keywords if normalize_text(keyword) in haystack]
    if not matched:
        return None
    if spec.match_mode == "all" and len(matched) != len(spec.keywords):
        return None
    return matched


def clean_html(value: str) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def parse_entry_datetime(entry: Any, timezone) -> datetime | None:
    raw = entry.get("published") or entry.get("updated")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(timezone)
        except (TypeError, ValueError, IndexError):
            pass
    parsed_struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed_struct:
        return datetime(*parsed_struct[:6], tzinfo=UTC).astimezone(timezone)
    return None


def source_name(entry: Any) -> str:
    source = entry.get("source")
    if source:
        source_title = source.get("title") if hasattr(source, "get") else str(source)
        if source_title:
            return clean_html(source_title)
    title = clean_html(entry.get("title", ""))
    # Google News usually provides only a title when a source element is missing.
    if " - " in title:
        return title.rsplit(" - ", 1)[-1]
    return urlparse(entry.get("link", "")).netloc or "알 수 없는 출처"


def source_title(entry: Any) -> str:
    title = clean_html(entry.get("title", ""))
    if " - " in title and entry.get("source"):
        return title.rsplit(" - ", 1)[0]
    return title


GOOGLE_NEWS_HOST = "news.google.com"
GOOGLE_BATCH_URL = "https://news.google.com/_/DotsSplashUi/data/batchexecute"

# 본문 추출이 이만큼도 안 나오면 로그인·차단 안내문일 가능성이 높다. 그럴 때는
# RSS 요약을 그대로 남긴다.
MIN_EXTRACTED_LENGTH = 120


def is_google_news(url: str) -> bool:
    return urlparse(url).netloc.endswith(GOOGLE_NEWS_HOST)


def _google_article_tokens(html: str) -> tuple[str, str, str] | None:
    values = []
    for attribute in ("data-n-a-id", "data-n-a-sg", "data-n-a-ts"):
        found = re.search(rf'{attribute}="([^"]+)"', html)
        if not found:
            return None
        values.append(found.group(1))
    return values[0], values[1], values[2]


async def resolve_google_news_url(client: httpx.AsyncClient, html: str) -> str | None:
    """Google 뉴스 중간 페이지에서 언론사 원문 주소를 얻는다.

    RSS의 `news.google.com/rss/articles/...` 링크는 서버 리다이렉트가 아니라
    자바스크립트 안내 페이지라서, 그대로 받으면 제목만 남고 본문이 사라진다.
    """
    tokens = _google_article_tokens(html)
    if not tokens:
        return None
    article_id, signature, timestamp = tokens
    try:
        request = json.dumps(
            [
                "garturlreq",
                [
                    ["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
                     None, None, None, None, None, 0, 1],
                    "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0,
                ],
                article_id,
                int(timestamp),
                signature,
            ]
        )
        response = await client.post(
            GOOGLE_BATCH_URL,
            data={"f.req": json.dumps([[["Fbv4je", request, None, "generic"]]])},
            headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        )
        if not response.is_success:
            return None
        for row in json.loads(response.text.split("\n", 1)[-1]):
            if len(row) > 2 and isinstance(row[2], str) and "garturlres" in row[2]:
                resolved = json.loads(row[2])[1]
                return resolved if isinstance(resolved, str) else None
    except (httpx.HTTPError, ValueError, TypeError, IndexError, KeyError):
        return None
    return None


# 본문 사이에 끼어드는 광고 자리표시 문구. 짧은 단독 줄일 때만 지운다.
AD_PLACEHOLDER = re.compile(
    r"^\s*(광고\s*(를)?\s*(불러오는\s*중|로딩|영역)?\s*[.…]*|advertisement|sponsored\s*content)\s*$",
    re.IGNORECASE,
)


def strip_ad_lines(text: str) -> str:
    kept = [line for line in text.splitlines() if not AD_PLACEHOLDER.match(line.strip()[:40])]
    return "\n".join(kept)


def extract_article_text(html: str) -> str:
    extracted = trafilatura.extract(
        html,
        output_format="txt",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )
    if extracted and len(extracted.strip()) >= 80:
        return extracted.strip()

    soup = BeautifulSoup(html, "html.parser")
    for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        element.decompose()
    main = soup.find("article") or soup.find("main") or soup.body or soup
    return main.get_text("\n", strip=True)


class GoogleNewsRssCollector:
    """RSS로 후보를 찾고, 원문 페이지에서 본문을 추출한다.

    RSS 발견 결과는 검색 엔진 색인에 좌우된다. 완전성을 요구하는 운영 환경에서는
    언론사 공식 RSS/API 어댑터를 이 클래스와 함께 추가해야 한다.
    """

    def __init__(self, settings: Settings):
        self.settings = settings

    async def collect(
        self,
        spec: SearchSpec,
        *,
        job_id: str,
        start: datetime,
        end: datetime,
        media_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        if not self.settings.rss_enabled or not spec.keywords:
            return []

        # Google News' day-based query syntax cannot express 05:00. Fetch the
        # whole final calendar day and apply the exact KST range below.
        query_end = end.date() + timedelta(days=1)
        window = f"after:{start.date().isoformat()} before:{query_end.isoformat()}"
        headers = {"User-Agent": self.settings.user_agent}
        timeout = httpx.Timeout(self.settings.request_timeout_seconds)
        async with httpx.AsyncClient(headers=headers, timeout=timeout, follow_redirects=True) as client:
            entries: dict[str, Any] = {}
            for query in spec.queries:
                response = await client.get(
                    "https://news.google.com/rss/search",
                    params={"q": f"{query} {window}", "hl": "ko", "gl": "KR", "ceid": "KR:ko"},
                )
                response.raise_for_status()
                feed = await asyncio.to_thread(feedparser.parse, response.content)
                # 검색식이 여러 개면 같은 기사가 겹친다. 링크 기준으로 한 번만 담는다.
                for entry in feed.entries:
                    entries.setdefault(entry.get("link", ""), entry)

            tasks = [
                self._build_article(client, entry, spec, job_id, start, end, media_dir)
                for entry in entries.values()
            ]
            built = await asyncio.gather(*tasks, return_exceptions=True)

        articles: list[dict[str, Any]] = []
        for candidate in built:
            if isinstance(candidate, dict):
                articles.append(candidate)
        return articles

    async def _build_article(
        self,
        client: httpx.AsyncClient,
        entry: Any,
        spec: SearchSpec,
        job_id: str,
        start: datetime,
        end: datetime,
        media_dir: Path | None,
    ) -> dict[str, Any] | None:
        published_at = parse_entry_datetime(entry, self.settings.timezone)
        if not published_at or not (start <= published_at < end):
            return None

        title = source_title(entry)
        original_url = entry.get("link", "")
        if not title or not original_url:
            return None

        summary = clean_html(entry.get("summary", ""))
        resolved_url = original_url
        content = summary
        article_html = ""
        try:
            response = await client.get(original_url)
            resolved_url = str(response.url)
            html = response.text if response.is_success else ""

            if html and is_google_news(resolved_url):
                publisher_url = await resolve_google_news_url(client, html)
                if publisher_url:
                    article_response = await client.get(publisher_url)
                    resolved_url = str(article_response.url)
                    html = article_response.text if article_response.is_success else ""

            if html and not is_google_news(resolved_url):
                article_html = html
                extracted = await asyncio.to_thread(extract_article_text, html)
                if len(extracted) >= MIN_EXTRACTED_LENGTH:
                    content = extracted
        except httpx.HTTPError:
            # A paywall or a transient failure should not erase a valid RSS candidate.
            pass

        # 매체를 한정한 갈래에서는 실제 원문 주소로 한 번 더 확인한다.
        # Google의 site: 검색만 믿으면 다른 매체의 전재 기사가 섞일 수 있다.
        if not spec.matches_site(resolved_url):
            return None

        content = re.sub(r"\n{3,}", "\n\n", strip_ad_lines(content)).strip()
        matched = match_keywords(spec, f"{title} {summary} {content[:4000]}")
        if matched is None:
            return None

        article_id = hashlib.sha256(f"{job_id}|{resolved_url}".encode("utf-8")).hexdigest()[:32]

        # 키워드 검증을 통과한 기사만 이미지를 내려받는다.
        images: list[dict[str, Any]] = []
        if media_dir is not None and article_html:
            images = await collect_article_images(
                client,
                html=article_html,
                base_url=resolved_url,
                article_id=article_id,
                destination=media_dir,
            )

        signature = normalize_text(f"{title} {summary} {content[:1200]}")
        return {
            "id": article_id,
            "job_id": job_id,
            "report_date": end.date().isoformat(),
            "title": title,
            # RSS가 이름 대신 호스트만 줄 때가 있다. 아는 매체면 제 이름으로 적는다.
            "publisher": publisher_for(resolved_url) or source_name(entry),
            "source_url": resolved_url,
            "published_at": published_at.isoformat(),
            "scraped_at": datetime.now(UTC).isoformat(),
            "summary": summary,
            "content": content or summary or title,
            "content_hash": hashlib.sha256(signature.encode("utf-8")).hexdigest(),
            "matched_keywords": matched,
            "preferred": 1 if spec.is_preferred(resolved_url) else 0,
            "duplicate_of": None,
            "images": images,
        }


class DuplicateDetector:
    """명백한 재전송 기사를 우선적으로 묶는 보수적인 중복 판정기."""

    @staticmethod
    def _is_duplicate(candidate: dict[str, Any], canonical: dict[str, Any]) -> bool:
        if candidate["content_hash"] == canonical["content_hash"]:
            return True

        title_score = fuzz.ratio(normalize_text(candidate["title"]), normalize_text(canonical["title"]))
        if title_score >= 94:
            return True

        left = normalize_text(candidate["content"])[:1800]
        right = normalize_text(canonical["content"])[:1800]
        if len(left) >= 240 and len(right) >= 240 and fuzz.ratio(left, right) >= 91:
            return True
        return False

    def mark(self, database: Database, job_id: str) -> tuple[int, int]:
        articles = database.articles(job_id, unique_only=False)
        # 같은 보도자료가 여러 곳에 실렸다면 지역 매체 판본을 먼저, 그다음 사진과
        # 본문이 가장 많이 남은 판본을 대표로 남긴다.
        ranked = sorted(
            articles,
            key=lambda article: (
                bool(article.get("preferred")),
                len(article.get("images") or []),
                len(article["content"]),
            ),
            reverse=True,
        )
        canonical_articles: list[dict[str, Any]] = []
        for article in ranked:
            duplicate_of = next(
                (
                    canonical["id"]
                    for canonical in canonical_articles
                    if self._is_duplicate(article, canonical)
                ),
                None,
            )
            database.set_duplicate(article["id"], duplicate_of)
            if duplicate_of is None:
                canonical_articles.append(article)
        return len(articles), len(canonical_articles)
