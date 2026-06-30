from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4
from typing import List, Optional

from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from app.models import ImportAsset, ImportEvent, ImportJob, User


@dataclass
class JobSnapshot:
    id: str
    archive_name: str
    state: str
    progress: float
    progress_label: str
    progress_current: int
    progress_total: int
    total_messages: int
    processed_messages: int
    sent_messages: int
    error_count: int
    current_item: str
    detail: str
    last_error: str
    logs: List[str]
    events: List[dict[str, str]]
    target_kind: str
    target_value: str
    additional_member_emails: list[str]
    resolved_chat_id: Optional[str]
    asset_total: int
    asset_processed: int
    asset_ready: int
    asset_failed: int
    asset_retryable: int
    asset_progress: float
    archive_available: bool
    can_pause: bool
    can_resume: bool
    can_retry: bool
    created_at: Optional[str]
    started_at: Optional[str]
    finished_at: Optional[str]


def create_job(
    db: Session,
    *,
    owner: User,
    archive_name: str,
    archive_path: Path,
    target_kind: str,
    target_value: str,
    additional_member_emails: list[str] | None = None,
) -> ImportJob:
    job = ImportJob(
        id=uuid4().hex,
        owner_id=owner.id,
        archive_name=archive_name,
        archive_path=str(archive_path),
        target_kind=target_kind,
        target_value=target_value,
        additional_member_emails=",".join(additional_member_emails or []),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return job


def append_event(db: Session, *, job: ImportJob, message: str, level: str = "info") -> None:
    db.add(ImportEvent(job_id=job.id, level=level, message=message))
    db.flush()


def list_jobs_for_user(db: Session, user: User) -> List[ImportJob]:
    query = select(ImportJob).order_by(desc(ImportJob.created_at))
    if not user.is_admin:
        query = query.where(ImportJob.owner_id == user.id)
    return db.execute(query.limit(50)).scalars().all()


def count_running_jobs(db: Session) -> int:
    return db.execute(select(func.count()).select_from(ImportJob).where(ImportJob.state == "running")).scalar_one()


def get_job_for_user(db: Session, *, job_id: str, user: User) -> Optional[ImportJob]:
    query = select(ImportJob).where(ImportJob.id == job_id)
    if not user.is_admin:
        query = query.where(ImportJob.owner_id == user.id)
    return db.execute(query).scalar_one_or_none()


def snapshot_job(db: Session, job: ImportJob) -> JobSnapshot:
    event_rows = db.execute(
        select(ImportEvent.level, ImportEvent.message)
        .where(ImportEvent.job_id == job.id)
        .order_by(desc(ImportEvent.created_at), desc(ImportEvent.id))
        .limit(12)
    ).all()
    logs = [message for _, message in event_rows]
    events = [{"level": level, "message": message} for level, message in event_rows]

    archive_available = bool(job.archive_path) and Path(job.archive_path).exists()
    asset_stats = db.execute(
        select(
            func.count(ImportAsset.id),
            func.sum(case((ImportAsset.public_url != "", 1), else_=0)),
            func.sum(case((ImportAsset.state == "failed", 1), else_=0)),
            func.sum(case((ImportAsset.link_sent.is_(True), 1), else_=0)),
        ).where(ImportAsset.job_id == job.id)
    ).one()
    asset_total = int(asset_stats[0] or 0)
    asset_ready = int(asset_stats[1] or 0)
    asset_failed = int(asset_stats[2] or 0)
    asset_linked = int(asset_stats[3] or 0)
    asset_processed = asset_ready + asset_failed
    asset_progress = round((asset_processed / asset_total) * 100, 1) if asset_total else 0.0
    asset_retryable = max(asset_total - asset_linked, 0)
    can_retry = (
        job.state in {"completed", "failed", "paused"}
        and archive_available
        and bool(job.resolved_chat_id)
        and asset_retryable > 0
    )
    message_progress = 0.0
    if job.total_messages:
        message_progress = round(job.processed_messages / job.total_messages * 100, 1)
    elif job.state in {"completed", "failed"}:
        message_progress = 100.0

    progress = message_progress
    progress_label = "Отправка сообщений"
    progress_current = job.processed_messages
    progress_total = job.total_messages
    if asset_total and job.processed_messages == 0 and asset_processed < asset_total:
        progress = asset_progress
        progress_label = "Получение ссылок"
        progress_current = asset_processed
        progress_total = asset_total
    elif asset_total and job.processed_messages == 0 and asset_processed >= asset_total and job.total_messages:
        progress = 0.0
        progress_label = "Отправка сообщений"
        progress_current = 0
        progress_total = job.total_messages

    return JobSnapshot(
        id=job.id,
        archive_name=job.archive_name,
        state=job.state,
        progress=progress,
        progress_label=progress_label,
        progress_current=progress_current,
        progress_total=progress_total,
        total_messages=job.total_messages,
        processed_messages=job.processed_messages,
        sent_messages=job.sent_messages,
        error_count=job.error_count,
        current_item=job.current_item,
        detail=job.detail,
        last_error=job.last_error,
        logs=list(logs),
        events=events,
        target_kind=job.target_kind,
        target_value=job.target_value,
        additional_member_emails=[item.strip() for item in (job.additional_member_emails or "").split(",") if item.strip()],
        resolved_chat_id=job.resolved_chat_id,
        asset_total=asset_total,
        asset_processed=asset_processed,
        asset_ready=asset_ready,
        asset_failed=asset_failed,
        asset_retryable=asset_retryable,
        asset_progress=asset_progress,
        archive_available=archive_available,
        can_pause=job.state in {"queued", "running", "pause_requested"},
        can_resume=job.state in {"paused", "failed"} and archive_available,
        can_retry=can_retry,
        created_at=job.created_at.isoformat() if job.created_at else None,
        started_at=job.started_at.isoformat() if job.started_at else None,
        finished_at=job.finished_at.isoformat() if job.finished_at else None,
    )
