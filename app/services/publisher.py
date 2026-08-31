from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from app.config import PROJECT_ROOT, Settings
from app.database import Database
from app.services.site_builder import build_site


PAGES_BRANCH = "gh-pages"
WORKTREE = PROJECT_ROOT / ".gitworktrees" / PAGES_BRANCH
GIT_TIMEOUT = 90

# 인증 정보가 없을 때 git이 입력창을 띄우고 멈추면 요청이 끝나지 않는다.
# 물어보지 말고 바로 실패하게 해서, 화면에 원인을 알려 준다.
NON_INTERACTIVE = {
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
}


class PublishError(RuntimeError):
    """게시 도중 멈춘 이유를 화면에 그대로 전할 때 쓴다."""


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-c", "credential.interactive=false", *args],
            cwd=str(cwd or PROJECT_ROOT),
            check=check,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=GIT_TIMEOUT,
            env={**os.environ, **NON_INTERACTIVE},
        )
    except subprocess.TimeoutExpired as error:
        raise PublishError("git 명령이 응답하지 않습니다. GitHub 인증 상태를 확인하세요.") from error
    except subprocess.CalledProcessError as error:
        raise PublishError((error.stderr or error.stdout or str(error)).strip()) from error


def pages_url() -> str | None:
    """원격 주소에서 GitHub Pages 주소를 유추한다."""
    remote = git("remote", "get-url", "origin", check=False)
    if remote.returncode != 0:
        return None
    found = re.search(r"github\.com[:/]([^/]+)/([^/.]+)", remote.stdout.strip())
    if not found:
        return None
    owner, repo = found.group(1), found.group(2)
    return f"https://{owner}.github.io/{repo}/"


def _ensure_worktree() -> None:
    """gh-pages 브랜치를 별도 폴더로 꺼내 둔다. 없으면 빈 브랜치로 만든다."""
    if (WORKTREE / ".git").exists():
        git("fetch", "origin", PAGES_BRANCH, check=False)
        git("reset", "--hard", f"origin/{PAGES_BRANCH}", cwd=WORKTREE, check=False)
        return

    WORKTREE.parent.mkdir(parents=True, exist_ok=True)
    if WORKTREE.exists():
        shutil.rmtree(WORKTREE)

    git("fetch", "origin", PAGES_BRANCH, check=False)
    on_remote = git("ls-remote", "--exit-code", "--heads", "origin", PAGES_BRANCH, check=False)
    if on_remote.returncode == 0:
        git("worktree", "add", str(WORKTREE), "-B", PAGES_BRANCH, f"origin/{PAGES_BRANCH}")
    else:
        # 소스 이력과 섞이지 않도록 부모 없는 브랜치로 시작한다.
        git("worktree", "add", "--detach", str(WORKTREE))
        git("checkout", "--orphan", PAGES_BRANCH, cwd=WORKTREE)
        git("rm", "-rf", ".", cwd=WORKTREE, check=False)


def _copy_site(site: Path) -> None:
    for existing in WORKTREE.iterdir():
        if existing.name == ".git":
            continue
        shutil.rmtree(existing) if existing.is_dir() else existing.unlink()
    shutil.copytree(site, WORKTREE, dirs_exist_ok=True)


def publish(
    database: Database, settings: Settings, *, push: bool = True
) -> dict[str, Any]:
    """승인분을 정적 사이트로 만들고 gh-pages 브랜치에 올린다."""
    site = settings.data_dir / "site"
    built = build_site(database, settings, site)
    result: dict[str, Any] = {
        **built,
        "committed": False,
        "pushed": False,
        "pages_url": pages_url(),
        "message": "",
    }

    if not (PROJECT_ROOT / ".git").exists():
        raise PublishError("git 저장소가 아닙니다. 먼저 저장소를 연결하세요.")

    _ensure_worktree()
    _copy_site(site)
    git("add", "-A", cwd=WORKTREE)
    if not git("status", "--porcelain", cwd=WORKTREE).stdout.strip():
        result["message"] = "바뀐 내용이 없어 그대로 두었습니다."
        return result

    message = f"사이트 갱신 {built['built_at']} (최신 {built['latest_date']})"
    git("commit", "-m", message, cwd=WORKTREE)
    result["committed"] = True

    if not push:
        result["message"] = "커밋까지 마쳤습니다. 푸시는 아직입니다."
        return result

    pushed = git("push", "origin", PAGES_BRANCH, cwd=WORKTREE, check=False)
    if pushed.returncode != 0:
        detail = (pushed.stderr or pushed.stdout).strip().splitlines()
        raise PublishError(
            "푸시에 실패했습니다. GitHub 인증을 마친 뒤 다시 시도하세요. "
            + (detail[-1] if detail else "")
        )
    result["pushed"] = True
    result["message"] = "GitHub Pages에 게시했습니다."
    return result
