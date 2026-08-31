from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings


HEADING_MARK = re.compile(r"^#{1,6}\s*")
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
        for article in articles[:20]:
            lines.append(f"- [{article['publisher']}] {article['title']} ({article['source_url']})")
        return "\n".join(lines)

    @staticmethod
    def _context(articles: list[dict[str, Any]]) -> str:
        blocks: list[str] = []
        for index, article in enumerate(articles[:24], start=1):
            content = (article["content"] or article["summary"]).strip().replace("\x00", " ")[:1800]
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
            "짧은 한국어 브리핑을 작성한다. 기사에 없는 숫자·인물·결론을 만들지 않는다. "
            "전체 분량은 공백 포함 600자를 넘기지 않는다. 각 항목 끝에는 근거 기사 번호를 "
            "[기사 N] 형식으로 붙인다. 마크다운으로 다음 순서를 지킨다.\n"
            "# 한눈에 보기 — 오늘의 흐름을 2~3줄로 요약한다.\n"
            "# 주요 내용 — 사안별로 '- '로 시작하는 항목을 최대 5개, 각 항목은 한두 문장으로 쓴다.\n"
            "이 두 항목만 쓴다. '확인 필요'처럼 추가 확인 사항을 정리하는 항목은 넣지 않는다.\n"
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
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        try:
            async with httpx.AsyncClient(timeout=self.settings.ollama_timeout_seconds) as client:
                response = await client.post(f"{self.settings.ollama_url}/api/chat", json=payload)
                response.raise_for_status()
                result = response.json()
            body = strip_dropped_sections((result.get("message") or {}).get("content", ""))
            if not body:
                raise ValueError("Ollama returned an empty message")
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
