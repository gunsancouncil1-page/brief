"""승인된 결과를 정적 사이트로 만들어 GitHub Pages에 올린다.

    python -m app.publish            사이트 생성 + gh-pages 갱신 + 푸시
    python -m app.publish --no-push  생성과 커밋까지만 (푸시는 나중에)
    python -m app.publish --build    파일만 만들고 git은 건드리지 않음

GitHub Pages는 정적 파일만 서비스한다. 수집·검토·승인은 이 PC에서 하고,
그 결과 화면만 여기서 내보낸다. 관리자 페이지는 올라가지 않는다.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from app.config import PROJECT_ROOT, load_settings
from app.database import Database
from app.services.site_builder import build_site


PAGES_BRANCH = "gh-pages"
WORKTREE = PROJECT_ROOT / ".gitworktrees" / PAGES_BRANCH


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or PROJECT_ROOT),
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def ensure_worktree() -> None:
    """gh-pages 브랜치를 별도 폴더로 꺼내 둔다. 없으면 빈 브랜치로 만든다."""
    if (WORKTREE / ".git").exists():
        git("fetch", "origin", PAGES_BRANCH, check=False)
        git("reset", "--hard", f"origin/{PAGES_BRANCH}", cwd=WORKTREE, check=False)
        return

    WORKTREE.parent.mkdir(parents=True, exist_ok=True)
    if WORKTREE.exists():
        shutil.rmtree(WORKTREE)

    git("fetch", "origin", PAGES_BRANCH, check=False)
    remote_has_branch = git(
        "ls-remote", "--exit-code", "--heads", "origin", PAGES_BRANCH, check=False
    ).returncode == 0

    if remote_has_branch:
        git("worktree", "add", str(WORKTREE), "-B", PAGES_BRANCH, f"origin/{PAGES_BRANCH}")
    else:
        # 소스 이력과 섞이지 않도록 부모 없는 브랜치로 시작한다.
        git("worktree", "add", "--detach", str(WORKTREE))
        git("checkout", "--orphan", PAGES_BRANCH, cwd=WORKTREE)
        git("rm", "-rf", ".", cwd=WORKTREE, check=False)


def copy_site(site: Path) -> None:
    for existing in WORKTREE.iterdir():
        if existing.name == ".git":
            continue
        shutil.rmtree(existing) if existing.is_dir() else existing.unlink()
    shutil.copytree(site, WORKTREE, dirs_exist_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="정적 사이트 생성 및 GitHub Pages 게시")
    parser.add_argument("--build", action="store_true", help="파일만 만들고 끝낸다")
    parser.add_argument("--no-push", action="store_true", help="커밋까지만 하고 푸시하지 않는다")
    args = parser.parse_args(argv)

    settings = load_settings()
    database = Database(settings.database_path)
    database.initialize()

    site = settings.data_dir / "site"
    result = build_site(database, settings, site)
    print(f"사이트 생성: {result['destination']}")
    print(f"  날짜 {len(result['dates'])}개, 최신 {result['latest_date']}, 기준 {result['built_at']}")
    if not result["dates"]:
        print("  승인된 자료가 없어 빈 사이트입니다. 관리자 페이지에서 승인 후 다시 실행하세요.")
    if args.build:
        return 0

    if not (PROJECT_ROOT / ".git").exists():
        print("git 저장소가 아닙니다. 먼저 저장소를 연결하세요.", file=sys.stderr)
        return 1

    ensure_worktree()
    copy_site(site)
    git("add", "-A", cwd=WORKTREE)
    if not git("status", "--porcelain", cwd=WORKTREE).stdout.strip():
        print("바뀐 내용이 없어 게시를 건너뜁니다.")
        return 0

    message = f"사이트 갱신 {result['built_at']} (최신 {result['latest_date']})"
    git("commit", "-m", message, cwd=WORKTREE)
    print(f"커밋: {message}")

    if args.no_push:
        print("푸시는 건너뛰었습니다. 나중에: git -C .gitworktrees/gh-pages push origin gh-pages")
        return 0

    pushed = git("push", "origin", PAGES_BRANCH, cwd=WORKTREE, check=False)
    if pushed.returncode != 0:
        print(pushed.stderr.strip(), file=sys.stderr)
        print("푸시에 실패했습니다. GitHub 인증을 마친 뒤 다시 실행하세요.", file=sys.stderr)
        return 1
    print("GitHub Pages에 게시했습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
