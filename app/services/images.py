from __future__ import annotations

import asyncio
import hashlib
import re
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura
from bs4 import BeautifulSoup
from bs4.element import Tag
from PIL import Image, UnidentifiedImageError


# 본문 사진으로 인정할 최소 조건. 배너·아이콘·추적 픽셀은 대부분 이 아래에서 걸린다.
MIN_BYTE_SIZE = 6 * 1024
MIN_WIDTH = 200
MIN_HEIGHT = 150
MAX_BYTE_SIZE = 12 * 1024 * 1024
MAX_IMAGES_PER_ARTICLE = 6
MAX_STORED_WIDTH = 1600
JPEG_QUALITY = 82

# 광고 사업자 도메인. 본문 안에 삽입돼 있어도 기사 사진이 아니다.
AD_HOSTS = (
    "doubleclick", "googlesyndication", "googleadservices", "adservice", "adsystem",
    "adnxs", "criteo", "taboola", "outbrain", "adop", "dable", "mediacategory",
    "zamccm", "widerplanet", "cauly", "adpies", "mobon", "ad.kakao", "adcr.naver",
)

# 파일 경로에 이런 조각이 있으면 기사 사진이 아니다.
AD_URL_WORDS = (
    "banner", "advert", "sponsor", "promo", "popup", "logo", "icon", "sprite",
    "spacer", "blank", "pixel", "emoticon", "btn_", "_btn", "button", "watermark",
    "share", "sns_", "profile", "avatar", "placeholder", "loading", "dummy",
)
AD_URL_PATTERN = re.compile(r"(?:^|[/_.-])ads?(?:[/_.-]|$)", re.IGNORECASE)

# 이런 컨테이너 안의 이미지는 본문 사진이 아니다.
AD_CLASS_PATTERN = re.compile(
    r"^(ads?|adv|advert\w*|banner|sponsor\w*|promo\w*|popup|paid)([-_]\w*)?$", re.IGNORECASE
)
BOILERPLATE_CLASS_WORDS = (
    "advertis", "banner", "sponsor", "related", "recommend", "footer", "header",
    "gnb", "lnb", "nav", "aside", "sidebar", "share", "sns", "comment", "reply",
    "popular", "ranking", "widget", "outbrain", "taboola",
)

LAZY_ATTRIBUTES = ("src", "data-src", "data-original", "data-lazy-src", "data-echo", "data-url")

ARTICLE_CONTAINER_HINTS = (
    "article", "main", '[itemprop="articleBody"]', "#articleBody", "#article-body",
    "#articleContent", "#news_body_area", ".article-body", ".article_body",
    ".news-content", "#textinput",
)


def _looks_like_ad_url(url: str) -> bool:
    lowered = url.lower()
    host = urlparse(lowered).netloc
    if any(marker in host for marker in AD_HOSTS):
        return True
    path = urlparse(lowered).path
    if any(word in path for word in AD_URL_WORDS):
        return True
    return bool(AD_URL_PATTERN.search(path))


def _is_in_ad_container(tag: Tag) -> bool:
    for ancestor in list(tag.parents)[:6]:
        if not isinstance(ancestor, Tag):
            continue
        tokens = list(ancestor.get("class") or [])
        identifier = ancestor.get("id") or ""
        if identifier:
            tokens.append(identifier)
        for token in tokens:
            if AD_CLASS_PATTERN.match(token):
                return True
            lowered = token.lower()
            if any(word in lowered for word in BOILERPLATE_CLASS_WORDS):
                return True
    return False


def _image_source(tag: Tag) -> str:
    for attribute in LAZY_ATTRIBUTES:
        value = (tag.get(attribute) or "").strip()
        # A lazy-loading placeholder is usually a data URI or a 1px gif.
        if value and not value.startswith("data:"):
            return value
    srcset = (tag.get("srcset") or "").strip()
    if srcset:
        return srcset.split(",")[0].strip().split(" ")[0]
    return ""


def _caption(tag: Tag) -> str:
    for attribute in ("alt", "title", "data-caption"):
        value = " ".join((tag.get(attribute) or "").split())
        if value and len(value) <= 200:
            return value
    return ""


def body_images(html: str, base_url: str) -> list[dict[str, str]]:
    """기사 본문 안의 사진 주소만 골라낸다.

    1차 판단은 trafilatura의 본문 추출에 맡긴다. 광고·내비게이션·추천 기사 블록을
    이미 걷어낸 결과이므로, 남은 <graphic>은 대체로 기사 사진이다. 본문 추출이
    이미지를 못 찾았을 때만 기사 컨테이너를 직접 훑는다.
    """
    found: list[tuple[str, str]] = []

    extracted = trafilatura.extract(
        html,
        output_format="xml",
        include_images=True,
        include_comments=False,
        favor_precision=True,
    )
    if extracted:
        for graphic in BeautifulSoup(extracted, "html.parser").find_all("graphic"):
            source = (graphic.get("src") or "").strip()
            if source:
                found.append((source, _caption(graphic)))

    if not found:
        soup = BeautifulSoup(html, "html.parser")
        container = None
        for hint in ARTICLE_CONTAINER_HINTS:
            container = soup.select_one(hint)
            if container:
                break
        for image in (container or soup).find_all("img"):
            if _is_in_ad_container(image):
                continue
            source = _image_source(image)
            if source:
                found.append((source, _caption(image)))

    images: list[dict[str, str]] = []
    seen: set[str] = set()
    for source, caption in found:
        absolute = urljoin(base_url, source)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        if _looks_like_ad_url(absolute) or absolute in seen:
            continue
        seen.add(absolute)
        images.append({"url": absolute, "caption": caption})
    return images[:MAX_IMAGES_PER_ARTICLE]


def body_image_urls(html: str, base_url: str) -> list[str]:
    return [image["url"] for image in body_images(html, base_url)]


def _normalize(payload: bytes) -> tuple[bytes, int, int] | None:
    """유효한 사진이면 JPEG로 통일해 돌려준다. 아니면 None."""
    try:
        with Image.open(BytesIO(payload)) as image:
            image.load()
            width, height = image.size
            if width < MIN_WIDTH or height < MIN_HEIGHT:
                return None
            # A very wide, very short image is a banner, not a press photo.
            if width / max(height, 1) > 5 or height / max(width, 1) > 5:
                return None
            converted = image.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError):
        return None

    if converted.width > MAX_STORED_WIDTH:
        ratio = MAX_STORED_WIDTH / converted.width
        converted = converted.resize(
            (MAX_STORED_WIDTH, max(1, round(converted.height * ratio))), Image.LANCZOS
        )
    buffer = BytesIO()
    converted.save(buffer, format="JPEG", quality=JPEG_QUALITY, optimize=True)
    return buffer.getvalue(), converted.width, converted.height


async def collect_article_images(
    client: httpx.AsyncClient,
    *,
    html: str,
    base_url: str,
    article_id: str,
    destination: Path,
) -> list[dict[str, Any]]:
    """본문 이미지를 내려받아 JPEG로 저장하고 메타데이터를 돌려준다."""
    candidates = body_images(html, base_url)
    if not candidates:
        return []

    destination.mkdir(parents=True, exist_ok=True)
    images: list[dict[str, Any]] = []
    for position, candidate in enumerate(candidates):
        url = candidate["url"]
        try:
            response = await client.get(url, headers={"Referer": base_url})
            if not response.is_success:
                continue
            content_type = response.headers.get("content-type", "")
            payload = response.content
            if content_type and not content_type.startswith("image/"):
                continue
            if not (MIN_BYTE_SIZE <= len(payload) <= MAX_BYTE_SIZE):
                continue
        except httpx.HTTPError:
            continue

        normalized = await asyncio.to_thread(_normalize, payload)
        if normalized is None:
            continue
        data, width, height = normalized

        image_id = hashlib.sha256(f"{article_id}|{url}".encode("utf-8")).hexdigest()[:32]
        path = destination / f"{image_id}.jpg"
        path.write_bytes(data)
        images.append(
            {
                "id": image_id,
                "article_id": article_id,
                "source_url": url,
                "caption": candidate["caption"],
                "path": str(path),
                "width": width,
                "height": height,
                "byte_size": len(data),
                "position": position,
            }
        )
    return images
