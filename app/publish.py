"""승인된 결과를 정적 사이트로 만들어 GitHub Pages에 올린다.

    python -m app.publish            사이트 생성 + gh-pages 갱신 + 푸시
    python -m app.publish --no-push  생성과 커밋까지만 (푸시는 나중에)
    python -m app.publish --build    파일만 만들고 git은 건드리지 않음

관리자 페이지의 '사이트 게시' 버튼도 같은 일을 한다.
"""

from __future__ import annotations

import argparse
import sys

from app.config import load_settings
from app.database import Database
from app.services.publisher import PublishError, publish
from app.services.site_builder import build_site


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정적 사이트 생성 및 GitHub Pages 게시")
    parser.add_argument("--build", action="store_true", help="파일만 만들고 끝낸다")
    parser.add_argument("--no-push", action="store_true", help="커밋까지만 하고 푸시하지 않는다")
    args = parser.parse_args(argv)

    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()

    if args.build:
        built = build_site(database, settings, settings.data_dir / "site")
        print(f"사이트 생성: {built['destination']}")
        print(f"  날짜 {len(built['dates'])}개, 최신 {built['latest_date']}, 기준 {built['built_at']}")
        return 0

    try:
        result = publish(database, settings, push=not args.no_push)
    except PublishError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"사이트 생성: {result['destination']}")
    print(f"  날짜 {len(result['dates'])}개, 최신 {result['latest_date']}, 기준 {result['built_at']}")
    if not result["dates"]:
        print("  승인된 자료가 없어 빈 사이트입니다. 관리자 페이지에서 승인 후 다시 실행하세요.")
    print(result["message"] or "완료")
    if result["pushed"] and result["pages_url"]:
        print(f"  {result['pages_url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
