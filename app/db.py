from __future__ import annotations

import logging
import time
from contextlib import contextmanager

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker

from app.config import get_settings


settings = get_settings()
logger = logging.getLogger(__name__)
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()


def init_db() -> None:
    from app import models  # noqa: F401

    deadline = time.monotonic() + max(settings.db_startup_timeout, 0)
    attempt = 0

    while True:
        attempt += 1
        try:
            Base.metadata.create_all(bind=engine)
            _run_lightweight_migrations()
            return
        except OperationalError:
            if time.monotonic() >= deadline:
                raise
            logger.warning(
                "Database is not ready yet. Retrying in %.1fs (attempt %s).",
                settings.db_retry_interval,
                attempt,
            )
            time.sleep(max(settings.db_retry_interval, 0.1))


def _run_lightweight_migrations() -> None:
    from app import models

    inspector = inspect(engine)
    try:
        asset_columns = {column["name"] for column in inspector.get_columns("import_assets")}
    except Exception:
        asset_columns = set()

    if "link_sent" not in asset_columns:
        ddl = (
            "ALTER TABLE import_assets "
            "ADD COLUMN link_sent BOOLEAN NOT NULL DEFAULT FALSE"
        )
        with engine.begin() as connection:
            connection.execute(text(ddl))

    try:
        job_columns = {column["name"] for column in inspector.get_columns("import_jobs")}
    except Exception:
        job_columns = set()

    if "additional_member_emails" not in job_columns:
        ddl = (
            "ALTER TABLE import_jobs "
            "ADD COLUMN additional_member_emails TEXT NOT NULL DEFAULT ''"
        )
        with engine.begin() as connection:
            connection.execute(text(ddl))

    try:
        tables = set(inspector.get_table_names())
    except Exception:
        tables = set()

    if "import_message_refs" not in tables:
        models.ImportMessageRef.__table__.create(bind=engine, checkfirst=True)
    else:
        try:
            message_ref_columns = {column["name"]: column for column in inspector.get_columns("import_message_refs")}
        except Exception:
            message_ref_columns = {}

        for column_name in ("telegram_message_id", "yandex_message_id", "thread_id"):
            column_info = message_ref_columns.get(column_name) or {}
            column_type = str(column_info.get("type", "")).lower()
            if "bigint" in column_type:
                continue
            ddl = (
                f"ALTER TABLE import_message_refs "
                f"ALTER COLUMN {column_name} TYPE BIGINT"
                f" USING {column_name}::BIGINT"
            )
            with engine.begin() as connection:
                connection.execute(text(ddl))

    if "upload_sessions" not in tables:
        models.UploadSession.__table__.create(bind=engine, checkfirst=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def session_scope():
    db: Session = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
