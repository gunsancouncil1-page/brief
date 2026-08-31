from __future__ import annotations

import hashlib
import hmac
import secrets
import time


SESSION_COOKIE = "gunsan_admin"


def _signature(admin_key: str, expires_at: int) -> str:
    return hmac.new(
        admin_key.encode("utf-8"), f"admin|{expires_at}".encode("utf-8"), hashlib.sha256
    ).hexdigest()


def issue_session(admin_key: str, valid_hours: float) -> tuple[str, int]:
    """관리자 키를 확인한 뒤 발급하는 만료 서명 토큰."""
    max_age = int(valid_hours * 3600)
    expires_at = int(time.time()) + max_age
    return f"{expires_at}.{_signature(admin_key, expires_at)}", max_age


def verify_session(admin_key: str, token: str) -> bool:
    if not admin_key or not token or "." not in token:
        return False
    raw_expiry, _, signature = token.partition(".")
    try:
        expires_at = int(raw_expiry)
    except ValueError:
        return False
    if expires_at < time.time():
        return False
    return secrets.compare_digest(signature, _signature(admin_key, expires_at))
