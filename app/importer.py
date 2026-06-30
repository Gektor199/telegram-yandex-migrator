from __future__ import annotations

import time
import unicodedata
from contextlib import suppress
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from zipfile import ZipFile
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import SessionLocal
from app.env_store import write_env_file
from app.models import ImportAsset, ImportEvent, ImportJob, ImportMessageRef
from app.telegram_export import AttachmentSpec, TelegramArchiveError, TelegramMessage, parse_telegram_archive
from app.yandex_disk import YandexDiskClient, YandexDiskError
from app.yandex_messenger import SentTextResult, YandexMessengerClient, YandexMessengerError


class TelegramToYandexImporter:
    def __init__(self, *, yandex_client: YandexMessengerClient, disk_client: YandexDiskClient | None = None) -> None:
        self.yandex_client = yandex_client
        self.disk_client = disk_client

    def run_job(self, *, job_id: str, archive_path: Path, bot_token: str) -> None:
        db: Session = SessionLocal()
        try:
            settings = get_settings()
            if not bot_token.strip():
                raise YandexMessengerError("В админке или .env не задан OAuth-токен бота Яндекс Мессенджера.", fatal=True)
            job = db.get(ImportJob, job_id)
            if job is None:
                return

            self._update_job(db, job, detail="Читаю архив Telegram", state="running")
            try:
                export = parse_telegram_archive(archive_path)
            except TelegramArchiveError:
                self._cleanup_archive(archive_path)
                raise
            if not export.messages:
                self._cleanup_archive(archive_path)
                raise TelegramArchiveError("В архиве нет сообщений для импорта.")

            owner_login = self.yandex_client.ensure_org_login(job.owner.email)
            additional_member_logins = [
                self.yandex_client.ensure_org_login(item)
                for item in (job.additional_member_emails or "").split(",")
                if item.strip()
            ]
            target_name = self.yandex_client.normalize_target_name(job.target_value)
            target_label = self.yandex_client.describe_target_kind(job.target_kind)
            disk_uploads_enabled = bool(
                self.disk_client and settings.yandex_disk_enabled and settings.yandex_disk_oauth_token.strip()
            )
            disk_chat_folder_path: Optional[str] = None

            if job.resolved_chat_id:
                chat_id = job.resolved_chat_id
                self._update_job(db, job, detail=f"Продолжаю импорт в существующий {target_label}")
                self._append_event(
                    db,
                    job_id=job.id,
                    message=f"Продолжаю импорт в существующий {target_label}: {job.target_value}",
                )
                db.commit()
            else:
                self._update_job(db, job, detail=f"Создаю новый {target_label} в Яндекс Мессенджере")
                chat_id = self.yandex_client.create_chat(
                    token=bot_token,
                    name=target_name,
                    description=self.yandex_client.build_chat_description(
                        source_title=export.title,
                        initiator=job.owner.email,
                    ),
                    is_channel=job.target_kind == "channel",
                    admins=[owner_login],
                )
                job.resolved_chat_id = chat_id
                db.commit()

            if disk_uploads_enabled:
                disk_chat_folder_path = self.disk_client.build_chat_folder_path(target_name)
                self._append_event(db, job_id=job.id, message=f"Папка на Яндекс Диске: {disk_chat_folder_path}")
                db.commit()

            job.total_messages = len(export.messages)
            job.detail = (
                f"Продолжаю с сообщения {job.processed_messages + 1} из {len(export.messages)}"
                if job.processed_messages
                else f"Найдено сообщений: {len(export.messages)}"
            )
            if not any(event.message == f"Источник: {export.title}" for event in job.events):
                self._append_event(db, job_id=job.id, message=f"Источник: {export.title}")
            if not any(event.message == f"Создан новый {target_label}: {target_name}" for event in job.events):
                self._append_event(db, job_id=job.id, message=f"Создан новый {target_label}: {target_name}")
            if not any(event.message == f"Новый chat_id: {chat_id}" for event in job.events):
                self._append_event(db, job_id=job.id, message=f"Новый chat_id: {chat_id}")
            if not any(event.message == f"Администратор: {owner_login}" for event in job.events):
                self._append_event(db, job_id=job.id, message=f"Администратор: {owner_login}")
            db.commit()

            attachments_disabled_reason: Optional[str] = None
            skipped_attachments = 0
            with ZipFile(archive_path) as archive:
                member_map = self._build_member_map(archive.namelist())
                asset_rows: Dict[str, ImportAsset] = {}
                message_refs = self._load_message_refs(db=db, job=job)
                thread_roots = self._build_thread_roots(export.messages)
                if disk_uploads_enabled and disk_chat_folder_path:
                    asset_rows = self._ensure_asset_rows(db=db, job=job, messages=export.messages)
                    self._prepare_disk_assets(
                        db=db,
                        job=job,
                        archive=archive,
                        member_map=member_map,
                        messages=export.messages,
                        disk_token=settings.yandex_disk_oauth_token,
                        disk_chat_folder_path=disk_chat_folder_path,
                        disk_org_id=settings.yandex_disk_org_id,
                        asset_rows=asset_rows,
                    )
                    if job.processed_messages >= len(export.messages) and self._has_retryable_assets(asset_rows):
                        self._retry_missing_disk_links(
                            db=db,
                            job=job,
                            archive=archive,
                            member_map=member_map,
                            messages=export.messages,
                            chat_id=chat_id,
                            bot_token=bot_token,
                            disk_token=settings.yandex_disk_oauth_token,
                            disk_chat_folder_path=disk_chat_folder_path,
                            disk_org_id=settings.yandex_disk_org_id,
                            asset_rows=asset_rows,
                            message_refs=message_refs,
                            thread_roots=thread_roots,
                        )
                        job = db.get(ImportJob, job_id)
                        if job is None:
                            return
                        job.state = "completed"
                        job.finished_at = datetime.now(timezone.utc)
                        job.detail = (
                            f"Повторная отправка ссылок завершена. Ошибок: {job.error_count}"
                            if job.error_count
                            else "Повторная отправка ссылок завершена без ошибок"
                        )
                        self._ensure_additional_members_added(
                            db=db,
                            job=job,
                            bot_token=bot_token,
                            chat_id=chat_id,
                            is_channel=job.target_kind == "channel",
                            additional_member_logins=additional_member_logins,
                        )
                        db.commit()
                        return
                start_index = min(job.processed_messages, len(export.messages))
                for index, message in enumerate(export.messages[start_index:], start=start_index + 1):
                    job = db.get(ImportJob, job_id)
                    if job is None:
                        return
                    if job.state == "pause_requested":
                        job.state = "paused"
                        job.detail = f"Импорт остановлен пользователем на {job.processed_messages} из {job.total_messages}"
                        job.finished_at = datetime.now(timezone.utc)
                        db.commit()
                        return

                    job.current_item = self._message_label(index=index, total=len(export.messages), message=message)
                    job.detail = f"Отправка {index} из {len(export.messages)}"
                    db.flush()

                    if message.attachment and disk_uploads_enabled and disk_chat_folder_path:
                        asset = self._resolve_uploaded_asset(asset_rows, message.attachment)
                        attachment_public_url = asset.public_url.strip() if asset else ""
                        if not attachment_public_url:
                            skipped_attachments += 1
                        try:
                            sent_ref = self._send_text_parts(
                                message=message,
                                chat_id=chat_id,
                                bot_token=bot_token,
                                attachment_public_url=attachment_public_url or None,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                            self._store_message_ref(
                                db=db,
                                job=job,
                                message=message,
                                sent_ref=sent_ref,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                            if attachment_public_url and asset:
                                asset.link_sent = True
                                db.flush()
                        except (YandexDiskError, YandexMessengerError) as exc:
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(
                                    message=message,
                                    error_message=f"{exc}. Файл: {message.attachment.filename}",
                                ),
                            )
                            if isinstance(exc, YandexMessengerError) and exc.fatal:
                                raise
                            time.sleep(0.05)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(
                                    message=message,
                                    error_message=f"{exc}. Файл: {message.attachment.filename}",
                                ),
                            )
                            time.sleep(0.05)
                            continue
                    elif message.attachment:
                        if attachments_disabled_reason:
                            try:
                                sent_ref = self._send_text_parts(
                                    message=message,
                                    chat_id=chat_id,
                                    bot_token=bot_token,
                                    message_refs=message_refs,
                                    thread_roots=thread_roots,
                                )
                                self._store_message_ref(
                                    db=db,
                                    job=job,
                                    message=message,
                                    sent_ref=sent_ref,
                                    message_refs=message_refs,
                                    thread_roots=thread_roots,
                                )
                            except YandexMessengerError as exc:
                                self._mark_message_error(
                                    db,
                                    job,
                                    self._format_message_error(message=message, error_message=str(exc)),
                                )
                                if exc.fatal:
                                    raise
                                time.sleep(0.05)
                                continue
                            except Exception as exc:  # noqa: BLE001
                                self._mark_message_error(
                                    db,
                                    job,
                                    self._format_message_error(message=message, error_message=str(exc)),
                                )
                                time.sleep(0.05)
                                continue
                            skipped_attachments += 1
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(
                                    message=message,
                                    error_message=f"{attachments_disabled_reason} Файл: {message.attachment.filename}",
                                ),
                            )
                            time.sleep(0.05)
                            continue

                        try:
                            sent_ref = self._send_text_parts(
                                message=message,
                                chat_id=chat_id,
                                bot_token=bot_token,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                            self._store_message_ref(
                                db=db,
                                job=job,
                                message=message,
                                sent_ref=sent_ref,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                        except YandexMessengerError as exc:
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(message=message, error_message=str(exc)),
                            )
                            if exc.fatal:
                                raise
                            time.sleep(0.05)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(message=message, error_message=str(exc)),
                            )
                            time.sleep(0.05)
                            continue
                        try:
                            self._send_attachment(
                                archive=archive,
                                member_map=member_map,
                                message=message,
                                chat_id=chat_id,
                                bot_token=bot_token,
                            )
                        except YandexMessengerError as exc:
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(
                                    message=message,
                                    error_message=f"{exc}. Файл: {message.attachment.filename}",
                                ),
                            )
                            if exc.is_upload_quota_error():
                                attachments_disabled_reason = (
                                    "Загрузка вложений отключена из-за ограничения квоты файлов Яндекс Мессенджера."
                                )
                                self._append_event(db, job_id=job.id, message=attachments_disabled_reason, level="error")
                                db.commit()
                            elif exc.fatal and not exc.is_attachment_issue():
                                raise
                            time.sleep(0.05)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(
                                    message=message,
                                    error_message=f"{exc}. Файл: {message.attachment.filename}",
                                ),
                            )
                            time.sleep(0.05)
                            continue
                    else:
                        try:
                            sent_ref = self._send_text_parts(
                                message=message,
                                chat_id=chat_id,
                                bot_token=bot_token,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                            self._store_message_ref(
                                db=db,
                                job=job,
                                message=message,
                                sent_ref=sent_ref,
                                message_refs=message_refs,
                                thread_roots=thread_roots,
                            )
                        except YandexMessengerError as exc:
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(message=message, error_message=str(exc)),
                            )
                            if exc.fatal:
                                raise
                            time.sleep(0.05)
                            continue
                        except Exception as exc:  # noqa: BLE001
                            self._mark_message_error(
                                db,
                                job,
                                self._format_message_error(message=message, error_message=str(exc)),
                            )
                            time.sleep(0.05)
                            continue

                    job.processed_messages += 1
                    job.sent_messages += 1
                    db.commit()

                    time.sleep(0.05)

            job = db.get(ImportJob, job_id)
            if job is None:
                return
            job.state = "completed"
            job.finished_at = datetime.now(timezone.utc)
            if skipped_attachments:
                self._append_event(
                    db,
                    job_id=job.id,
                    message=f"Пропущено вложений во время отправки сообщений: {skipped_attachments}",
                    level="error",
                )
            self._ensure_additional_members_added(
                db=db,
                job=job,
                bot_token=bot_token,
                chat_id=chat_id,
                is_channel=job.target_kind == "channel",
                additional_member_logins=additional_member_logins,
            )
            job.detail = (
                f"Импорт завершен: {job.sent_messages} сообщений отправлено, ошибок: {job.error_count}"
                if job.error_count
                else f"Импорт завершен: {job.sent_messages} сообщений отправлено без ошибок"
            )
            db.commit()
        except (TelegramArchiveError, YandexMessengerError) as exc:
            self._mark_failed(db, job_id=job_id, error_message=str(exc), archive_path=archive_path)
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(db, job_id=job_id, error_message=f"Импорт остановлен: {exc}", archive_path=archive_path)
        finally:
            db.close()

    def _send_text_parts(
        self,
        *,
        message: TelegramMessage,
        chat_id: str,
        bot_token: str,
        attachment_public_url: Optional[str] = None,
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> Optional[SentTextResult]:
        sent_ref: Optional[SentTextResult] = None
        reply_message_id, thread_id = self._resolve_reply_context(
            message=message,
            message_refs=message_refs,
            thread_roots=thread_roots,
        )
        for part_index, text in enumerate(
            self._build_text_parts(message, attachment_public_url=attachment_public_url),
            start=1,
        ):
            payload_id = f"tg-{message.telegram_id}-{part_index}"
            current_reply_message_id = reply_message_id if part_index == 1 else None
            current_thread_id = thread_id if thread_id else None
            result = self.yandex_client.send_text(
                token=bot_token,
                chat_id=chat_id,
                text=text,
                payload_id=payload_id,
                reply_message_id=current_reply_message_id,
                thread_id=current_thread_id,
            )
            if sent_ref is None and result.message_id:
                sent_ref = result
                if thread_id is None:
                    thread_id = result.thread_id
        return sent_ref

    def _send_attachment(
        self,
        *,
        archive: ZipFile,
        member_map: Dict[str, str],
        message: TelegramMessage,
        chat_id: str,
        bot_token: str,
    ) -> None:
        if not message.attachment:
            return

        member = self._resolve_member(member_map, message.attachment.archive_path)
        if not member:
            raise FileNotFoundError(f"В архиве не найден файл {message.attachment.archive_path}")
        content = archive.read(member)
        self.yandex_client.send_file(
            token=bot_token,
            chat_id=chat_id,
            filename=message.attachment.filename,
            content=content,
            content_type=message.attachment.mime_type,
            is_image=message.attachment.is_image,
        )

    def _load_asset_rows(
        self,
        *,
        db: Session,
        job: ImportJob,
    ) -> Dict[str, ImportAsset]:
        return {
            asset.archive_path: asset
            for asset in db.execute(select(ImportAsset).where(ImportAsset.job_id == job.id)).scalars().all()
        }

    def _load_message_refs(
        self,
        *,
        db: Session,
        job: ImportJob,
    ) -> Dict[int, ImportMessageRef]:
        return {
            item.telegram_message_id: item
            for item in db.execute(select(ImportMessageRef).where(ImportMessageRef.job_id == job.id)).scalars().all()
        }

    def _ensure_asset_rows(
        self,
        *,
        db: Session,
        job: ImportJob,
        messages: Iterable[TelegramMessage],
    ) -> Dict[str, ImportAsset]:
        asset_rows = self._load_asset_rows(db=db, job=job)
        created = False
        for attachment in self._collect_unique_attachments(messages):
            if attachment.archive_path in asset_rows:
                continue
            asset = ImportAsset(
                job_id=job.id,
                archive_path=attachment.archive_path,
                filename=attachment.filename,
                state="pending",
            )
            db.add(asset)
            asset_rows[attachment.archive_path] = asset
            created = True
        if created:
            db.commit()
            asset_rows = self._load_asset_rows(db=db, job=job)
        return asset_rows

    def _has_retryable_assets(self, asset_rows: Dict[str, ImportAsset]) -> bool:
        return any(not asset.link_sent for asset in asset_rows.values())

    def _ensure_uploaded_asset(
        self,
        *,
        db: Session,
        job: ImportJob,
        archive: ZipFile,
        member_map: Dict[str, str],
        attachment: AttachmentSpec,
        disk_token: str,
        disk_chat_folder_path: str,
        disk_org_id: str,
        asset_rows: Dict[str, ImportAsset],
        force: bool = False,
    ) -> Optional[ImportAsset]:
        asset = asset_rows.get(attachment.archive_path)
        if asset is None:
            asset = ImportAsset(
                job_id=job.id,
                archive_path=attachment.archive_path,
                filename=attachment.filename,
            )
            db.add(asset)
            db.commit()
            db.refresh(asset)
            asset_rows[attachment.archive_path] = asset

        if asset.public_url.strip() and not force:
            return asset

        member = self._resolve_member(member_map, attachment.archive_path)
        if not member:
            self._mark_asset_failed(
                db,
                job=job,
                asset=asset,
                error_message=f"Файл не найден в архиве: {attachment.archive_path}",
            )
            return None

        try:
            member_info = archive.getinfo(member)
            size_bytes = int(getattr(member_info, "file_size", 0) or 0)
        except KeyError:
            self._mark_asset_failed(
                db,
                job=job,
                asset=asset,
                error_message=f"Файл не найден в архиве: {attachment.archive_path}",
            )
            return None

        job.current_item = f"Файл на Диске · {attachment.filename}"
        job.detail = f"Готовлю ссылку для вложения: {attachment.filename}"
        db.flush()

        asset.filename = attachment.filename
        asset.size_bytes = size_bytes
        asset.state = "uploading"
        asset.disk_path = ""
        asset.public_url = ""
        asset.last_error = ""
        db.flush()

        try:
            uploaded = self.disk_client.upload_public_file_stream(
                token=disk_token,
                chat_folder_path=disk_chat_folder_path,
                relative_archive_path=attachment.archive_path,
                stream_factory=lambda: archive.open(member),
                expected_size=size_bytes,
                content_type=attachment.mime_type,
                org_id=disk_org_id,
            )
        except YandexDiskError as exc:
            if exc.is_insufficient_storage():
                self._mark_asset_failed(
                    db,
                    job=job,
                    asset=asset,
                    error_message=f"Не удалось подготовить ссылку для файла {attachment.filename}: {exc}",
                )
                self._activate_disk_full_maintenance(
                    db=db,
                    current_job=job,
                    reason=(
                        "На Яндекс Диске технической учётной записи закончилось место. "
                        "Сервис переведён в режим сервисных работ."
                    ),
                )
                raise YandexDiskError(
                    "На Яндекс Диске технической учётной записи закончилось место. "
                    "Сервис переведён в режим сервисных работ."
                ) from exc
            self._mark_asset_failed(
                db,
                job=job,
                asset=asset,
                error_message=f"Не удалось подготовить ссылку для файла {attachment.filename}: {exc}",
            )
            return None

        asset.state = "uploaded"
        asset.disk_path = uploaded.path
        asset.public_url = uploaded.public_url
        asset.last_error = ""
        self._append_event(db, job_id=job.id, message=f"Подготовлена ссылка для файла: {attachment.filename}")
        db.commit()
        return asset

    def _mark_asset_failed(self, db: Session, *, job: ImportJob, asset: ImportAsset, error_message: str) -> None:
        asset.state = "failed"
        asset.last_error = error_message
        job.error_count += 1
        job.last_error = error_message
        db.add(ImportEvent(job_id=job.id, message=error_message, level="error"))
        db.commit()

    def _activate_disk_full_maintenance(self, *, db: Session, current_job: ImportJob, reason: str) -> None:
        settings = get_settings()
        if not settings.maintenance_mode:
            write_env_file(settings.env_file_path, {"MAINTENANCE_MODE": "true"})

        now = datetime.now(timezone.utc)
        stop_message = reason
        active_jobs = (
            db.execute(
                select(ImportJob).where(
                    ImportJob.state.in_(("queued", "running", "pause_requested")),
                    ImportJob.id != current_job.id,
                )
            )
            .scalars()
            .all()
        )
        for job in active_jobs:
            if job.state == "queued":
                job.state = "paused"
                job.finished_at = now
            else:
                job.state = "pause_requested"
            job.detail = stop_message
            job.last_error = stop_message
            db.add(ImportEvent(job_id=job.id, message=stop_message, level="error"))
        db.add(ImportEvent(job_id=current_job.id, message=stop_message, level="error"))
        db.commit()

    def _mark_job_error(self, db: Session, *, job: ImportJob, error_message: str) -> None:
        job.error_count += 1
        job.last_error = error_message
        db.add(ImportEvent(job_id=job.id, message=error_message, level="error"))
        db.commit()

    def _ensure_additional_members_added(
        self,
        *,
        db: Session,
        job: ImportJob,
        bot_token: str,
        chat_id: str,
        is_channel: bool,
        additional_member_logins: List[str],
    ) -> None:
        if not additional_member_logins:
            return
        if any(event.message.startswith("Добавлены участники:") for event in job.events):
            return
        job.current_item = "Добавляю сотрудников в новый чат"
        job.detail = "Добавляю сотрудников в чат после завершения переноса сообщений"
        db.flush()
        try:
            self.yandex_client.add_members(
                token=bot_token,
                chat_id=chat_id,
                is_channel=is_channel,
                members=additional_member_logins,
            )
        except YandexMessengerError as exc:
            added_logins: List[str] = []
            failed_logins: List[str] = []
            failed_details: List[str] = []
            for login in additional_member_logins:
                try:
                    self.yandex_client.add_members(
                        token=bot_token,
                        chat_id=chat_id,
                        is_channel=is_channel,
                        members=[login],
                    )
                    added_logins.append(login)
                except YandexMessengerError as member_exc:
                    failed_logins.append(login)
                    failed_details.append(f"{login}: {member_exc}")

            if added_logins:
                self._append_event(db, job_id=job.id, message=f"Добавлены участники: {', '.join(added_logins)}")
                db.commit()

            if failed_logins:
                self._mark_job_error(
                    db,
                    job=job,
                    error_message=(
                        "Не удалось добавить сотрудников в чат: "
                        f"{', '.join(failed_logins)}. "
                        f"Ответ Яндекса на batch: {exc}. "
                        f"Подробности: {'; '.join(failed_details)}"
                    ),
                )
            return
        self._append_event(db, job_id=job.id, message=f"Добавлены участники: {', '.join(additional_member_logins)}")
        db.commit()

    def _prepare_disk_assets(
        self,
        *,
        db: Session,
        job: ImportJob,
        archive: ZipFile,
        member_map: Dict[str, str],
        messages: Iterable[TelegramMessage],
        disk_token: str,
        disk_chat_folder_path: str,
        disk_org_id: str,
        asset_rows: Dict[str, ImportAsset],
    ) -> None:
        attachments = self._collect_unique_attachments(messages)
        total_assets = len(attachments)
        if not total_assets:
            return

        for index, attachment in enumerate(attachments, start=1):
            if job.state == "pause_requested":
                return
            asset = asset_rows.get(attachment.archive_path)
            if asset and asset.public_url.strip():
                continue
            job.current_item = f"Файл на Диске · {attachment.filename}"
            job.detail = f"Получаю ссылки: {index} из {total_assets}"
            db.flush()
            self._ensure_uploaded_asset(
                db=db,
                job=job,
                archive=archive,
                member_map=member_map,
                attachment=attachment,
                disk_token=disk_token,
                disk_chat_folder_path=disk_chat_folder_path,
                disk_org_id=disk_org_id,
                asset_rows=asset_rows,
            )

    def _retry_missing_disk_links(
        self,
        *,
        db: Session,
        job: ImportJob,
        archive: ZipFile,
        member_map: Dict[str, str],
        messages: Iterable[TelegramMessage],
        chat_id: str,
        bot_token: str,
        disk_token: str,
        disk_chat_folder_path: str,
        disk_org_id: str,
        asset_rows: Dict[str, ImportAsset],
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> None:
        retry_messages = [message for message in messages if message.attachment and not asset_rows[message.attachment.archive_path].link_sent]
        total_retry = len(retry_messages)
        for index, message in enumerate(retry_messages, start=1):
            attachment = message.attachment
            if not attachment:
                continue
            asset = asset_rows[attachment.archive_path]
            job.current_item = f"Повторная ссылка · {attachment.filename}"
            job.detail = f"Повторно отправляю ссылки: {index} из {total_retry}"
            db.flush()

            if not asset.public_url.strip():
                refreshed = self._ensure_uploaded_asset(
                    db=db,
                    job=job,
                    archive=archive,
                    member_map=member_map,
                    attachment=attachment,
                    disk_token=disk_token,
                    disk_chat_folder_path=disk_chat_folder_path,
                    disk_org_id=disk_org_id,
                    asset_rows=asset_rows,
                    force=True,
                )
                if refreshed is None or not refreshed.public_url.strip():
                    continue
                asset = refreshed

            try:
                self._send_retry_attachment_link(
                    message=message,
                    chat_id=chat_id,
                    bot_token=bot_token,
                    attachment_public_url=asset.public_url,
                    message_refs=message_refs,
                    thread_roots=thread_roots,
                )
                asset.link_sent = True
                asset.last_error = ""
                db.flush()
            except YandexMessengerError as exc:
                self._mark_message_error(
                    db,
                    job,
                    self._format_message_error(
                        message=message,
                        error_message=f"{exc}. Файл: {attachment.filename}",
                    ),
                )
                if exc.fatal:
                    raise
            except Exception as exc:  # noqa: BLE001
                self._mark_message_error(
                    db,
                    job,
                    self._format_message_error(
                        message=message,
                        error_message=f"{exc}. Файл: {attachment.filename}",
                    ),
                )

    def _resolve_uploaded_asset(
        self,
        uploaded_assets: Dict[str, ImportAsset],
        attachment: Optional[AttachmentSpec],
    ) -> Optional[ImportAsset]:
        if not attachment:
            return None
        return uploaded_assets.get(attachment.archive_path)

    def _collect_unique_attachments(self, messages: Iterable[TelegramMessage]) -> List[AttachmentSpec]:
        unique: Dict[str, AttachmentSpec] = {}
        for message in messages:
            if not message.attachment:
                continue
            unique.setdefault(message.attachment.archive_path, message.attachment)
        return list(unique.values())

    def _build_text_parts(self, message: TelegramMessage, attachment_public_url: Optional[str] = None) -> List[str]:
        full_text = self._render_message_text(
            message=message,
            attachment_public_url=attachment_public_url,
        )
        if not full_text:
            return []
        return _split_text(full_text, limit=5800)

    def _send_retry_attachment_link(
        self,
        *,
        message: TelegramMessage,
        chat_id: str,
        bot_token: str,
        attachment_public_url: str,
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> None:
        if not message.attachment:
            return
        text = self._render_message_text(
            message=message,
            attachment_public_url=attachment_public_url,
            include_body=False,
        )
        reply_message_id, thread_id = self._resolve_retry_reply_context(
            message=message,
            message_refs=message_refs,
            thread_roots=thread_roots,
        )
        self.yandex_client.send_text(
            token=bot_token,
            chat_id=chat_id,
            text=text,
            payload_id=f"tg-retry-{message.telegram_id}",
            reply_message_id=reply_message_id,
            thread_id=thread_id,
        )

    def _message_label(self, *, index: int, total: int, message: TelegramMessage) -> str:
        author = "Система" if message.is_service else message.author
        return f"{index}/{total} · {author} · {message.date or 'без даты'}"

    def _store_message_ref(
        self,
        *,
        db: Session,
        job: ImportJob,
        message: TelegramMessage,
        sent_ref: Optional[SentTextResult],
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> None:
        if not sent_ref or not sent_ref.message_id:
            return

        existing = message_refs.get(message.telegram_id)
        if existing is None:
            existing = ImportMessageRef(
                job_id=job.id,
                telegram_message_id=message.telegram_id,
                yandex_message_id=sent_ref.message_id,
            )
            db.add(existing)
            message_refs[message.telegram_id] = existing

        root_telegram_id = thread_roots.get(message.telegram_id, message.telegram_id)
        if message.reply_to_id:
            parent_ref = message_refs.get(message.reply_to_id)
            root_ref = message_refs.get(root_telegram_id)
            existing.thread_id = (
                sent_ref.thread_id
                or getattr(root_ref, "thread_id", None)
                or getattr(root_ref, "yandex_message_id", None)
                or getattr(parent_ref, "thread_id", None)
                or getattr(parent_ref, "yandex_message_id", None)
            )
        else:
            existing.thread_id = sent_ref.thread_id or sent_ref.message_id

        existing.yandex_message_id = sent_ref.message_id
        db.flush()

    def _resolve_reply_context(
        self,
        *,
        message: TelegramMessage,
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> tuple[int | None, int | None]:
        if not message.reply_to_id:
            return None, None

        parent_ref = message_refs.get(message.reply_to_id)
        if parent_ref is None:
            return None, None

        root_telegram_id = thread_roots.get(message.telegram_id, message.reply_to_id)
        root_ref = message_refs.get(root_telegram_id)
        thread_id = (
            getattr(root_ref, "thread_id", None)
            or getattr(root_ref, "yandex_message_id", None)
            or getattr(parent_ref, "thread_id", None)
            or getattr(parent_ref, "yandex_message_id", None)
        )
        return None, thread_id

    def _resolve_retry_reply_context(
        self,
        *,
        message: TelegramMessage,
        message_refs: Dict[int, ImportMessageRef],
        thread_roots: Dict[int, int],
    ) -> tuple[int | None, int | None]:
        current_ref = message_refs.get(message.telegram_id)
        if current_ref is not None:
            return None, current_ref.thread_id or current_ref.yandex_message_id
        return self._resolve_reply_context(message=message, message_refs=message_refs, thread_roots=thread_roots)

    def _build_thread_roots(self, messages: Iterable[TelegramMessage]) -> Dict[int, int]:
        message_by_id = {message.telegram_id: message for message in messages}
        roots: Dict[int, int] = {}

        for telegram_id, message in message_by_id.items():
            roots[telegram_id] = self._find_thread_root_id(message=message, message_by_id=message_by_id)

        return roots

    def _find_thread_root_id(
        self,
        *,
        message: TelegramMessage,
        message_by_id: Dict[int, TelegramMessage],
    ) -> int:
        current = message
        visited: set[int] = set()
        while current.reply_to_id and current.reply_to_id in message_by_id and current.reply_to_id not in visited:
            visited.add(current.telegram_id)
            current = message_by_id[current.reply_to_id]
        return current.telegram_id

    def _render_message_text(
        self,
        *,
        message: TelegramMessage,
        attachment_public_url: Optional[str] = None,
        include_body: bool = True,
    ) -> str:
        sections: List[str] = []

        author = "Системное сообщение" if message.is_service else (message.author or "Неизвестный автор")
        timestamp = _format_message_datetime(message.date)
        header = author if not timestamp else f"{author} · {timestamp}"
        sections.append(header.strip())

        if include_body and message.text:
            sections.append(message.text.strip())

        if message.attachment:
            attachment_lines = [self._display_attachment_name(message.attachment.filename)]
            if attachment_public_url:
                attachment_lines.append(attachment_public_url.strip())
            elif not include_body:
                attachment_lines.append("Ссылка на файл будет отправлена повторно.")
            sections.append("\n".join(line for line in attachment_lines if line))

        return "\n\n".join(section for section in sections if section).strip()

    def _build_member_map(self, names: List[str]) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for name in names:
            normalized = _normalize_archive_path(name)
            for key in _archive_lookup_keys(normalized):
                mapping.setdefault(key, name)
            basename = normalized.rsplit("/", 1)[-1]
            for key in _archive_lookup_keys(basename):
                mapping.setdefault(f"basename:{key}", name)
            parts = normalized.split("/")
            for offset in range(1, len(parts)):
                suffix = "/".join(parts[offset:])
                for key in _archive_lookup_keys(suffix):
                    mapping.setdefault(key, name)
        return mapping

    def _resolve_member(self, member_map: Dict[str, str], archive_path: str) -> Optional[str]:
        normalized = _normalize_archive_path(archive_path)
        for key in _archive_lookup_keys(normalized):
            member = member_map.get(key)
            if member:
                return member
        basename = normalized.rsplit("/", 1)[-1]
        for key in _archive_lookup_keys(basename):
            member = member_map.get(f"basename:{key}")
            if member:
                return member
        return None

    def _append_event(self, db: Session, *, job_id: str, message: str, level: str = "info") -> None:
        db.add(ImportEvent(job_id=job_id, message=message, level=level))

    @staticmethod
    def _display_attachment_name(filename: str) -> str:
        cleaned = (filename or "").replace("@", " ")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        return cleaned or "Файл"

    def _update_job(self, db: Session, job: ImportJob, *, detail: str, state: Optional[str] = None) -> None:
        job.detail = detail
        if state is not None:
            job.state = state
        db.commit()

    def _mark_message_error(self, db: Session, job: ImportJob, error_message: str) -> None:
        job.processed_messages += 1
        job.error_count += 1
        job.last_error = error_message
        db.add(ImportEvent(job_id=job.id, message=error_message, level="error"))
        db.commit()

    def _mark_failed(self, db: Session, *, job_id: str, error_message: str, archive_path: Path) -> None:
        job = db.get(ImportJob, job_id)
        if job is None:
            return
        job.state = "failed"
        job.detail = error_message
        job.last_error = error_message
        job.finished_at = datetime.now(timezone.utc)
        db.add(ImportEvent(job_id=job.id, message=error_message, level="error"))
        db.commit()

    def _cleanup_archive(self, archive_path: Path) -> None:
        with suppress(FileNotFoundError):
            archive_path.unlink()

    def _format_message_error(self, *, message: TelegramMessage, error_message: str) -> str:
        return f"Сообщение #{message.telegram_id}: {error_message}"


def _split_text(text: str, *, limit: int) -> List[str]:
    if len(text) <= limit:
        return [text]

    chunks: List[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    if remaining:
        chunks.append(remaining)
    return chunks


def _format_message_datetime(raw_value: str) -> str:
    value = (raw_value or "").strip()
    if not value:
        return ""

    normalized = value.replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace(" ", "T", 1)):
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.strftime("%d.%m.%Y %H:%M")
        except ValueError:
            continue

    try:
        parsed = datetime.fromtimestamp(int(value), tz=timezone.utc)
        return parsed.strftime("%d.%m.%Y %H:%M")
    except (TypeError, ValueError, OSError):
        return value


def _normalize_archive_path(value: str) -> str:
    return value.strip().replace("\\", "/").lstrip("./")


def _archive_lookup_keys(value: str) -> List[str]:
    variants = {
        value,
        value.lower(),
    }

    for form in ("NFC", "NFD", "NFKC", "NFKD"):
        normalized = unicodedata.normalize(form, value)
        variants.add(normalized)
        variants.add(normalized.lower())

    for source_encoding in ("utf-8", "cp1251", "cp866", "mac_cyrillic", "koi8-r"):
        try:
            mojibake = value.encode(source_encoding).decode("cp437")
        except (UnicodeEncodeError, UnicodeDecodeError):
            continue
        variants.add(mojibake)
        variants.add(mojibake.lower())

    return [item for item in variants if item]
