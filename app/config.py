from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from app.env_store import read_env_file


def _split_csv(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


@dataclass
class Settings:
    host: str
    port: int
    app_name: str
    secret_key: str
    maintenance_mode: bool
    database_url: str
    db_startup_timeout: float
    db_retry_interval: float
    max_archive_size_mb: int
    env_file_path: Path
    data_dir: Path
    upload_dir: Path
    yandex_api_base: str
    bootstrap_yandex_bot_token: str
    yandex_disk_enabled: bool
    yandex_disk_api_base: str
    yandex_disk_oauth_token: str
    yandex_disk_client_id: str
    yandex_disk_client_secret: str
    yandex_disk_root_reference: str
    yandex_disk_org_id: str
    worker_poll_interval: float
    archive_retention_hours: int
    max_parallel_jobs: int
    max_parallel_jobs_per_user: int
    sso_enabled: bool
    oidc_server_metadata_url: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_scope: str
    admin_emails: list[str]
    admin_domains: list[str]
    public_base_url: str
    local_admin_login: str
    local_admin_password: str


def get_settings() -> Settings:
    env_file_path = Path(os.getenv("APP_ENV_FILE", ".env")).expanduser().resolve()
    file_values = read_env_file(env_file_path)

    def env_value(name: str, default: str) -> str:
        return str(file_values.get(name, os.getenv(name, default)))

    data_dir = Path(env_value("APP_DATA_DIR", "data")).resolve()
    upload_dir = data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    secret_key = env_value("SECRET_KEY", "").strip()
    if not secret_key or secret_key == "please-change-me":
        raise RuntimeError("SECRET_KEY must be explicitly set in .env or environment.")

    return Settings(
        host=env_value("HOST", "0.0.0.0"),
        port=int(env_value("PORT", "8080")),
        app_name=env_value("APP_NAME", "Telegram -> Yandex Migrator"),
        secret_key=secret_key,
        maintenance_mode=env_value("MAINTENANCE_MODE", "false").lower() in {"1", "true", "yes"},
        database_url=env_value("DATABASE_URL", "sqlite:///./data/app.db"),
        db_startup_timeout=float(env_value("DB_STARTUP_TIMEOUT", "60")),
        db_retry_interval=float(env_value("DB_RETRY_INTERVAL", "1")),
        max_archive_size_mb=int(env_value("MAX_ARCHIVE_SIZE_MB", "512")),
        env_file_path=env_file_path,
        data_dir=data_dir,
        upload_dir=upload_dir,
        yandex_api_base=env_value("YANDEX_API_BASE", "https://botapi.messenger.yandex.net/bot/v1").rstrip("/") + "/",
        bootstrap_yandex_bot_token=env_value("YANDEX_BOT_TOKEN", "").strip(),
        yandex_disk_enabled=env_value("YANDEX_DISK_ENABLED", "false").lower() in {"1", "true", "yes"},
        yandex_disk_api_base=env_value("YANDEX_DISK_API_BASE", "https://cloud-api.yandex.net/v1/disk").rstrip("/"),
        yandex_disk_oauth_token=env_value("YANDEX_DISK_OAUTH_TOKEN", "").strip(),
        yandex_disk_client_id=env_value("YANDEX_DISK_CLIENT_ID", "").strip(),
        yandex_disk_client_secret=env_value("YANDEX_DISK_CLIENT_SECRET", "").strip(),
        yandex_disk_root_reference=env_value("YANDEX_DISK_ROOT_REFERENCE", "").strip(),
        yandex_disk_org_id=env_value("YANDEX_DISK_ORG_ID", "").strip(),
        worker_poll_interval=float(env_value("WORKER_POLL_INTERVAL", "1.0")),
        archive_retention_hours=int(env_value("ARCHIVE_RETENTION_HOURS", "72")),
        max_parallel_jobs=int(env_value("MAX_PARALLEL_JOBS", "2")),
        max_parallel_jobs_per_user=int(env_value("MAX_PARALLEL_JOBS_PER_USER", "1")),
        sso_enabled=env_value("SSO_ENABLED", "true").lower() not in {"0", "false", "no"},
        oidc_server_metadata_url=env_value("OIDC_SERVER_METADATA_URL", "").strip(),
        oidc_client_id=env_value("OIDC_CLIENT_ID", "").strip(),
        oidc_client_secret=env_value("OIDC_CLIENT_SECRET", "").strip(),
        oidc_scope=env_value("OIDC_SCOPE", "openid profile email"),
        admin_emails=_split_csv(env_value("ADMIN_EMAILS", "")),
        admin_domains=_split_csv(env_value("ADMIN_DOMAINS", "")),
        public_base_url=env_value("PUBLIC_BASE_URL", "http://127.0.0.1:8080").rstrip("/"),
        local_admin_login=env_value("LOCAL_ADMIN_LOGIN", "admin").strip(),
        local_admin_password=env_value("LOCAL_ADMIN_PASSWORD", "").strip(),
    )
