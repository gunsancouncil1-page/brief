from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Section:
    """수집 대상 한 갈래. 화면 메뉴와 수집 작업이 이 정의를 함께 쓴다."""

    key: str
    label: str
    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    # 이 매체만 모은다. 비어 있으면 매체를 가리지 않는다.
    sites: tuple[str, ...]
    # 먼저 훑고 목록 위쪽에 배치할 매체. 다른 매체 기사도 함께 남는다.
    preferred_sites: tuple[str, ...]
    match_mode: str
    has_briefing: bool
    # True면 관리자가 검토·승인해야 공개된다. False면 수집 직후 바로 공개된다.
    requires_review: bool
    description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "keywords": list(self.keywords),
            "exclude_keywords": list(self.exclude_keywords),
            "sites": list(self.sites),
            "preferred_sites": list(self.preferred_sites),
            "match_mode": self.match_mode,
            "has_briefing": self.has_briefing,
            "requires_review": self.requires_review,
            "description": self.description,
        }


# 전북특별자치도의회와 도내 시·군의회. 군산시의회는 별도 갈래이므로 제외한다.
JEONBUK_COUNCILS: tuple[str, ...] = (
    "전북특별자치도의회",
    "전주시의회",
    "익산시의회",
    "정읍시의회",
    "남원시의회",
    "김제시의회",
    "완주군의회",
    "진안군의회",
    "무주군의회",
    "장수군의회",
    "임실군의회",
    "순창군의회",
    "고창군의회",
    "부안군의회",
)


# 군산시의회·군산시청 기사를 우선 훑을 지역 매체.
# 군산타임즈는 군산미래신문으로 법인이 바뀌어 같은 사이트를 쓴다.
LOCAL_PRESS: tuple[str, ...] = (
    "newsgunsan.com",              # 군산뉴스
    "gunsanews.com",               # 군산신문
    "gstimes.cyberstreet.co.kr",   # 군산타임즈 · 군산미래신문
    "todaygunsan.co.kr",           # 투데이군산
    "hansbiz.co.kr",               # 한스경제
    "jjan.kr",                     # 전북일보
    "jjn.co.kr",                   # 전북중앙
    "sjbnews.com",                 # 새전북신문
    "newsinjb.com",                # 뉴스인전북
    "domin.co.kr",                 # 전북도민일보
)


# Google 뉴스가 매체 이름을 주지 않을 때 쓸 이름표.
# 일부 매체는 www 없는 호스트(ww.newsgunsan.com 등)로도 기사를 낸다.
PUBLISHER_NAMES: dict[str, str] = {
    "newsgunsan.com": "군산뉴스",
    "gunsanews.com": "군산신문",
    "gstimes.cyberstreet.co.kr": "군산미래신문",
    "todaygunsan.co.kr": "투데이군산",
    "hansbiz.co.kr": "한스경제",
    "jjan.kr": "전북일보",
    "jjn.co.kr": "전북중앙",
    "sjbnews.com": "새전북신문",
    "newsinjb.com": "뉴스인전북",
    "domin.co.kr": "전북도민일보",
    "news.kbs.co.kr": "KBS 뉴스",
    "jmbc.co.kr": "전주MBC",
    "jtv.co.kr": "JTV 전주방송",
    "kcn.tv": "금강방송",
}


def publisher_for(url: str) -> str | None:
    """주소를 보고 아는 매체면 그 이름을 돌려준다."""
    host = url.split("//")[-1].split("/")[0].lower().split(":")[0]
    for domain, name in PUBLISHER_NAMES.items():
        if host == domain or host.endswith(f".{domain}"):
            return name
    return None


# 전북 지역 방송사. Google 뉴스는 매체를 site: 로 좁힐 수 있다.
# JTV(jtv.co.kr)는 현재 Google 뉴스에 기사가 색인되지 않아 실제로는 걸리지 않는다.
# 색인이 시작되면 설정을 바꾸지 않아도 함께 수집된다.
BROADCASTERS: tuple[str, ...] = (
    "news.kbs.co.kr",
    "jmbc.co.kr",
    "jtv.co.kr",
    "kcn.tv",
)


SECTIONS: dict[str, Section] = {
    "council": Section(
        key="council",
        label="군산시의회",
        keywords=("군산시의회",),
        exclude_keywords=(),
        sites=(),
        preferred_sites=LOCAL_PRESS,
        match_mode="any",
        has_briefing=True,
        requires_review=True,
        description="군산시의회 관련 보도자료. 지역 매체 우선. 검토·승인 후 공개",
    ),
    "cityhall": Section(
        key="cityhall",
        label="군산시청",
        keywords=("군산시청", "군산시"),
        exclude_keywords=("군산시의회",),
        sites=(),
        preferred_sites=LOCAL_PRESS,
        match_mode="any",
        has_briefing=True,
        requires_review=False,
        description="군산시청(집행부) 관련 보도자료. 지역 매체 우선. 수집 즉시 공개",
    ),
    "broadcast": Section(
        key="broadcast",
        label="방송소식",
        keywords=("군산",),
        exclude_keywords=(),
        sites=BROADCASTERS,
        preferred_sites=(),
        match_mode="any",
        has_briefing=False,
        requires_review=False,
        description="전주KBS·전주MBC·JTV·금강방송(KCN)의 군산 관련 보도. 수집 즉시 공개",
    ),
    "other_councils": Section(
        key="other_councils",
        label="타의회 보도자료",
        keywords=JEONBUK_COUNCILS,
        exclude_keywords=("군산시의회",),
        sites=(),
        preferred_sites=(),
        match_mode="any",
        has_briefing=False,
        requires_review=False,
        description="전북특별자치도의회와 도내 시·군의회 보도자료. 수집 즉시 공개",
    ),
}


@dataclass(frozen=True)
class MenuTab:
    key: str
    section: str
    view: str  # "briefing" | "articles"
    label: str


# 화면 상단 메뉴. 요구된 순서를 그대로 유지한다.
MENU: tuple[MenuTab, ...] = (
    MenuTab("council_briefing", "council", "briefing", "군산시의회 AI 브리핑"),
    MenuTab("council", "council", "articles", "군산시의회"),
    MenuTab("cityhall_briefing", "cityhall", "briefing", "군산시청 AI 브리핑"),
    MenuTab("cityhall", "cityhall", "articles", "군산시청"),
    MenuTab("broadcast", "broadcast", "articles", "방송소식"),
    MenuTab("other_councils", "other_councils", "articles", "타의회 보도자료"),
)


def menu_payload() -> list[dict[str, str]]:
    return [
        {
            "key": tab.key,
            "section": tab.section,
            "view": tab.view,
            "label": tab.label,
        }
        for tab in MENU
    ]
