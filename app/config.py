from __future__ import annotations

import logging
import sys
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./scraper.db"

    user_agent: str = "WebScraperBot/1.0 (+https://example.com/bot-info)"
    request_timeout: float = 20.0
    min_request_interval: float = 2.0  # per host
    max_retries: int = 3
    max_response_bytes: int = 5_000_000
    playwright_enabled: bool = True
    browser_timeout_ms: int = 30_000

    api_probe_enabled: bool = True
    pii_block_threshold: int = 25  # personal-data findings on one page that block the job
    presidio_enabled: bool = True
    presidio_spacy_model: str = "en_core_web_sm"

    llm_config_file: str = str(PROJECT_ROOT / "config" / "llm.toml")
    llm_profile: str | None = None  # overrides active_profile in the config file
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None

    llm_api_key: SecretStr | None = None
    openrouter_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    dashscope_api_key: SecretStr | None = None

    document_roots: list[str] = []  # empty means any folder is allowed
    max_document_bytes: int = 20_000_000
    max_documents_per_folder: int = 100
    upload_dir: str = str(PROJECT_ROOT / "uploads")

    # Keep false when deployed: otherwise the API can be used to probe internal networks.
    allow_private_hosts: bool = False

    min_item_confidence: float = 0.3

    use_celery: bool = False
    redis_url: str = "redis://localhost:6379/0"

    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]
    log_level: str = "INFO"
    audit_log_file: str | None = "logs/audit.log"


@lru_cache
def get_settings() -> Settings:
    return Settings()


_logging_configured = False


def setup_logging(settings: Settings | None = None) -> None:
    global _logging_configured
    if _logging_configured:
        return
    settings = settings or get_settings()
    formatter = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(settings.log_level.upper())

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    if settings.audit_log_file:
        path = Path(settings.audit_log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(path, maxBytes=10_000_000, backupCount=5, encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    for noisy in ("httpx", "httpcore", "presidio-analyzer", "openai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _logging_configured = True
