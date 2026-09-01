from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    timezone: ZoneInfo
    data_dir: Path
    schedule_enabled: bool
    auto_register: bool
    auto_publish: bool
    purge_previous_dates: bool
    admin_api_key: str
    admin_session_hours: float
    origin_shared_secret: str
    ollama_url: str
    ollama_model: str
    ollama_timeout_seconds: float
    rss_enabled: bool
    images_enabled: bool
    request_timeout_seconds: float
    user_agent: str

    @property
    def database_path(self) -> Path:
        return self.data_dir / "gunsan_brief.db"

    @property
    def media_dir(self) -> Path:
        """수집한 사진 등 기사에 딸린 파일을 두는 곳."""
        return self.data_dir / "media"


def load_settings() -> Settings:
    configured_dir = Path(os.getenv("DATA_DIR", "storage"))
    data_dir = configured_dir if configured_dir.is_absolute() else PROJECT_ROOT / configured_dir
    return Settings(
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        timezone=ZoneInfo(os.getenv("TIMEZONE", "Asia/Seoul")),
        data_dir=data_dir,
        schedule_enabled=_as_bool(os.getenv("SCHEDULE_ENABLED"), True),
        auto_register=_as_bool(os.getenv("AUTO_REGISTER"), True),
        auto_publish=_as_bool(os.getenv("AUTO_PUBLISH"), True),
        purge_previous_dates=_as_bool(os.getenv("PURGE_PREVIOUS_DATES"), True),
        admin_api_key=os.getenv("ADMIN_API_KEY", ""),
        admin_session_hours=float(os.getenv("ADMIN_SESSION_HOURS", "12")),
        origin_shared_secret=os.getenv("ORIGIN_SHARED_SECRET", ""),
        ollama_url=os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        ollama_timeout_seconds=float(os.getenv("OLLAMA_TIMEOUT_SECONDS", "240")),
        rss_enabled=_as_bool(os.getenv("RSS_ENABLED"), True),
        images_enabled=_as_bool(os.getenv("IMAGES_ENABLED"), False),
        request_timeout_seconds=float(os.getenv("REQUEST_TIMEOUT_SECONDS", "25")),
        user_agent=os.getenv("USER_AGENT", "GunsanBriefBot/0.1 (local research dashboard)"),
    )
