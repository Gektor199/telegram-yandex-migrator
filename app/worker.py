from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Thread

from sqlalchemy import asc, func, select

from app.config import get_settings
from app.db import SessionLocal, init_db
from app.importer import TelegramToYandexImporter
from app.models import ImportEvent
from app.models import ImportJob
from app.models import User
from app.settings_store import get_runtime_settings
from app.yandex_disk import YandexDiskClient
from app.yandex_messenger import YandexMessengerClient


def run_worker() -> None:
    init_db()
    active_threads: dict[str, Thread] = {}
    recovered_interrupted_jobs = False

    while True:
        settings = get_settings()
        completed = [job_id for job_id, thread in active_threads.items() if not thread.is_alive()]
        for job_id in completed:
            active_threads.pop(job_id, None)

        with SessionLocal() as db:
            if not recovered_interrupted_jobs:
                _recover_interrupted_jobs(db)
                recovered_interrupted_jobs = True

            runtime = get_runtime_settings(db, settings)
            max_parallel_jobs = max(1, int(runtime.get("max_parallel_jobs") or settings.max_parallel_jobs))
            max_parallel_jobs_per_user = max(
                1,
                int(runtime.get("max_parallel_jobs_per_user") or settings.max_parallel_jobs_per_user),
            )
            _cleanup_expired_archives(db, retention_hours=settings.archive_retention_hours)

            while len(active_threads) < max_parallel_jobs:
                job = _pick_next_queued_job(
                    db,
                    max_parallel_jobs_per_user=max_parallel_jobs_per_user,
                    maintenance_mode=settings.maintenance_mode,
                )
                if job is None:
                    break

                job.state = "running"
                job.detail = "Задача передана worker-процессу"
                job.started_at = datetime.now(timezone.utc)
                db.commit()

                client = YandexMessengerClient(api_base=runtime.get("yandex_api_base") or settings.yandex_api_base)
                disk_client = (
                    YandexDiskClient(
                        api_base=settings.yandex_disk_api_base,
                        root_reference=settings.yandex_disk_root_reference,
                    )
                    if settings.yandex_disk_enabled
                    else None
                )
                importer = TelegramToYandexImporter(yandex_client=client, disk_client=disk_client)
                thread = Thread(
                    target=importer.run_job,
                    kwargs={
                        "job_id": job.id,
                        "archive_path": Path(job.archive_path),
                        "bot_token": runtime.get("yandex_bot_token") or settings.bootstrap_yandex_bot_token,
                    },
                    daemon=True,
                )
                thread.start()
                active_threads[job.id] = thread

        time.sleep(settings.worker_poll_interval)


def _pick_next_queued_job(db, *, max_parallel_jobs_per_user: int, maintenance_mode: bool) -> ImportJob | None:
    stmt = (
        select(ImportJob)
        .join(User, User.id == ImportJob.owner_id)
        .where(ImportJob.state == "queued")
        .order_by(asc(ImportJob.created_at))
        .limit(100)
    )
    if maintenance_mode:
        stmt = stmt.where(User.is_admin.is_(True))

    queued_jobs = db.execute(stmt).scalars().all()

    for job in queued_jobs:
        owner_running_jobs = db.execute(
            select(func.count())
            .select_from(ImportJob)
            .where(
                ImportJob.owner_id == job.owner_id,
                ImportJob.state.in_(("running", "pause_requested")),
            )
        ).scalar_one()
        if owner_running_jobs < max_parallel_jobs_per_user:
            return job

    return None


def _recover_interrupted_jobs(db) -> None:
    stuck_jobs = (
        db.execute(select(ImportJob).where(ImportJob.state.in_(("running", "pause_requested"))))
        .scalars()
        .all()
    )
    if not stuck_jobs:
        return

    now = datetime.now(timezone.utc)
    for job in stuck_jobs:
        archive_exists = bool(job.archive_path) and Path(job.archive_path).exists()
        job.state = "paused" if archive_exists else "failed"
        job.detail = (
            "Worker был перезапущен. Задачу можно продолжить."
            if archive_exists
            else "Worker был перезапущен, а архив уже недоступен. Загрузите его заново."
        )
        job.last_error = "" if archive_exists else job.detail
        job.finished_at = now
        db.add(
            ImportEvent(
                job_id=job.id,
                level="error" if not archive_exists else "info",
                message=job.detail,
            )
        )
    db.commit()


def _cleanup_expired_archives(db, *, retention_hours: int) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max(retention_hours, 1))
    jobs = (
        db.execute(
            select(ImportJob).where(
                ImportJob.archive_path != "",
                ImportJob.state.in_(("completed", "failed", "paused")),
                ImportJob.finished_at.is_not(None),
                ImportJob.finished_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    if not jobs:
        return

    for job in jobs:
        if job.archive_path:
            Path(job.archive_path).unlink(missing_ok=True)
            job.archive_path = ""
    db.commit()


if __name__ == "__main__":
    run_worker()
