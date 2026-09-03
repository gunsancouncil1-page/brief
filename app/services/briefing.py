from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings


HEADING_MARK = re.compile(r"^#{1,6}\s*")

# 한 번에 모델에게 넘길 본문 총량(글자)과 모델에게 열어 줄 문맥 크기.
# 이 한도를 넘기면 Ollama가 앞쪽부터 잘라 내어, 지시문과 첫 기사들이 통째로 사라진다.
CONTEXT_BUDGET = 24000
MAX_ARTICLES = 60
MODEL_CONTEXT = 32768
# 모델이 지시를 어기고 붙이는 경우가 있어, 저장 전에 한 번 더 걷어낸다.
DROPPED_SECTION = re.compile(r"^#{1,6}\s*(확인\s*필요|확인이\s*필요한\s*점|추가\s*확인)", re.IGNORECASE)


def strip_dropped_sections(body: str) -> str:
    kept: list[str] = []
    skipping = False
    for line in body.splitlines():
        stripped = line.strip()
        if HEADING_MARK.match(stripped):
            skipping = bool(DROPPED_SECTION.match(stripped))
        if not skipping:
            kept.append(line)
    return "\n".join(kept).strip()


CITATION = re.compile(r"\[기사\s*([\d,\s가-힣]+?)\]")
CITED_NUMBER = re.compile(r"\d+")


def cited_articles(body: str) -> set[int]:
    """브리핑이 근거로 밝힌 기사 번호. '[기사 1, 기사 3]'처럼 묶어 쓴 것도 센다."""
    found: set[int] = set()
    for group in CITATION.findall(body):
        found.update(int(number) for number in CITED_NUMBER.findall(group))
    return found


def append_uncited(body: str, articles: list[dict[str, Any]]) -> str:
    """브리핑이 건너뛴 기사를 제목만이라도 마지막에 보탠다.

    "전체 기사를 종합적으로"가 이 탭의 약속이다. 모델이 몇 건을 빠뜨렸다고
    그 기사가 조용히 사라지면 안 된다.
    """
    cited = cited_articles(body)
    missing = [
        (index, article)
        for index, article in enumerate(articles[:MAX_ARTICLES], start=1)
        if index not in cited
    ]
    if not missing:
        return body
    lines = [body.rstrip()]
    for index, article in missing:
        lines.append(f"- {article['title']} ({article['publisher']}) [기사 {index}]")
    return "\n".join(lines)


class BriefingService:
    def __init__(self, settings: Settings):
        self.settings = settings

    @staticmethod
    def _fallback(job: dict[str, Any], articles: list[dict[str, Any]], reason: str) -> str:
        lines = [
            f"# {job['name']} 수집 현황",
            f"중복 제거 후 {len(articles)}건이 확인되었습니다.",
            "로컬 LLM 응답을 받지 못해 자동 브리핑 대신 확인 목록을 제공합니다.",
            f"사유: {reason}",
            "# 확인할 기사",
        ]
        for article in articles[:MAX_ARTICLES]:
            lines.append(f"- [{article['publisher']}] {article['title']} ({article['source_url']})")
        return "\n".join(lines)

    @staticmethod
    def _context(articles: list[dict[str, Any]]) -> str:
        chosen = articles[:MAX_ARTICLES]
        # 기사가 많은 날은 기사마다 본문을 짧게 잘라 전체가 한 번에 들어가게 한다.
        # 몇 건만 길게 넣으면 나머지가 잘려 브리핑에서 아예 빠진다.
        # 기사마다 제목·매체·주소 줄이 따라붙으므로 그 몫을 미리 뺀다.
        per_article = max(350, CONTEXT_BUDGET // max(len(chosen), 1) - 200)
        blocks: list[str] = []
        for index, article in enumerate(chosen, start=1):
            content = (
                (article["content"] or article["summary"]).strip().replace("\x00", " ")[:per_article]
            )
            blocks.append(
                "\n".join(
                    [
                        f"[기사 {index}]",
                        f"제목: {article['title']}",
                        f"언론사: {article['publisher']}",
                        f"발행시각: {article['published_at']}",
                        f"원문: {article['source_url']}",
                        f"본문: {content}",
                    ]
                )
            )
        return "\n\n".join(blocks)

    async def create(self, job: dict[str, Any], articles: list[dict[str, Any]]) -> dict[str, str]:
        generated_at = datetime.now(UTC).isoformat()
        if not articles:
            return {
                "body": "# 수집 결과\n\n해당 시간창에서 중복 제거 후 남은 보도자료가 없습니다.",
                "status": "complete",
                "model": self.settings.ollama_model,
                "generated_at": generated_at,
            }

        system = (
            "당신은 지방자치단체 보도자료를 검토하는 분석 보조자다. 제공된 기사만 근거로 "
            "한국어 브리핑을 작성한다. 기사에 없는 숫자·인물·결론을 만들지 않는다.\n"
            "제공된 기사를 하나도 빠뜨리지 말고 모두 반영한다. 같은 사안을 여러 매체가 "
            "다뤘으면 한 항목으로 묶고 근거 기사 번호를 함께 적는다. 각 항목 끝에는 근거를 "
            "[기사 N] 형식으로 붙인다.\n"
            "마크다운으로 다음 두 항목만, 이 순서로 쓴다.\n"
            "# 한눈에 보기 — 그날 전체 흐름을 3~5줄로 요약한다.\n"
            "# 주요 내용 — 사안별로 '- '로 시작하는 항목을 쓴다. 기사에 나온 사안은 모두 "
            "담되, 한 항목은 한두 문장으로 짧게 쓴다.\n"
            "'확인 필요'처럼 추가 확인 사항을 정리하는 항목은 넣지 않는다.\n"
            "수식어와 배경 설명은 빼고 결정·수치·일정 위주로 쓴다."
        )
        user = (
            f"대상: {job['name']}\n"
            f"검색 키워드: {', '.join(job['keywords'])}\n"
            f"기준일: {job['report_date']}\n"
            f"중복 제거 기사 {len(articles)}건\n\n{self._context(articles)}"
        )
        payload = {
            "model": self.settings.ollama_model,
            "stream": False,
            "think": False,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "options": {"temperature": 0.2, "num_ctx": MODEL_CONTEXT},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.ollama_timeout_seconds) as client:
                response = await client.post(f"{self.settings.ollama_url}/api/chat", json=payload)
                response.raise_for_status()
                result = response.json()
            body = strip_dropped_sections((result.get("message") or {}).get("content", ""))
            if not body:
                raise ValueError("Ollama returned an empty message")
            body = append_uncited(body, articles)
            return {
                "body": body,
                "status": "complete",
                "model": self.settings.ollama_model,
                "generated_at": generated_at,
            }
        except (httpx.HTTPError, ValueError, KeyError) as error:
            return {
                "body": self._fallback(job, articles, type(error).__name__),
                "status": "fallback",
                "model": self.settings.ollama_model,
                "generated_at": generated_at,
            }
