from __future__ import annotations

from datetime import datetime
from typing import Optional
from uuid import uuid4

from sqlalchemy import BIGINT, Boolean, DateTime, ForeignKey, Integer, Text, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def generate_uuid() -> str:
    return uuid4().hex


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    external_subject: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    full_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    jobs: Mapped[list["ImportJob"]] = relationship(back_populates="owner")
    upload_sessions: Mapped[list["UploadSession"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class ImportJob(Base):
    __tablename__ = "import_jobs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=generate_uuid)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    archive_name: Mapped[str] = mapped_column(Text, nullable=False)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="queued", index=True)
    target_kind: Mapped[str] = mapped_column(Text, nullable=False, default="chat")
    target_value: Mapped[str] = mapped_column(Text, nullable=False)
    additional_member_emails: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolved_chat_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    total_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    sent_messages: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_item: Mapped[str] = mapped_column(Text, nullable=False, default="")
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="Ожидает запуска")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped["User"] = relationship(back_populates="jobs")
    events: Mapped[list["ImportEvent"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    assets: Mapped[list["ImportAsset"]] = relationship(back_populates="job", cascade="all, delete-orphan")
    message_refs: Mapped[list["ImportMessageRef"]] = relationship(back_populates="job", cascade="all, delete-orphan")


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=generate_uuid)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    archive_name: Mapped[str] = mapped_column(Text, nullable=False)
    temp_path: Mapped[str] = mapped_column(Text, nullable=False)
    total_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    received_bytes: Mapped[int] = mapped_column(BIGINT, nullable=False, default=0)
    state: Mapped[str] = mapped_column(Text, nullable=False, default="uploading", index=True)
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    owner: Mapped["User"] = relationship(back_populates="upload_sessions")


class ImportEvent(Base):
    __tablename__ = "import_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id"), nullable=False, index=True)
    level: Mapped[str] = mapped_column(Text, nullable=False, default="info")
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    job: Mapped["ImportJob"] = relationship(back_populates="events")


class ImportAsset(Base):
    __tablename__ = "import_assets"
    __table_args__ = (UniqueConstraint("job_id", "archive_path", name="uq_import_assets_job_archive_path"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id"), nullable=False, index=True)
    archive_path: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    disk_path: Mapped[str] = mapped_column(Text, nullable=False, default="")
    public_url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    state: Mapped[str] = mapped_column(Text, nullable=False, default="pending", index=True)
    link_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    job: Mapped["ImportJob"] = relationship(back_populates="assets")


class ImportMessageRef(Base):
    __tablename__ = "import_message_refs"
    __table_args__ = (UniqueConstraint("job_id", "telegram_message_id", name="uq_import_message_refs_job_tg_message"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("import_jobs.id"), nullable=False, index=True)
    telegram_message_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    yandex_message_id: Mapped[int] = mapped_column(BIGINT, nullable=False)
    thread_id: Mapped[Optional[int]] = mapped_column(BIGINT, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    job: Mapped["ImportJob"] = relationship(back_populates="message_refs")


class AppSetting(Base):
    __tablename__ = "app_settings"
    __table_args__ = (UniqueConstraint("key", name="uq_app_settings_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_secret: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    updated_by_user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id"), nullable=True)
