from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional
from zipfile import ZipFile


class TelegramArchiveError(Exception):
    """Raised when Telegram Desktop export cannot be parsed."""


@dataclass
class AttachmentSpec:
    archive_path: str
    filename: str
    mime_type: str
    is_image: bool


@dataclass
class TelegramMessage:
    telegram_id: int
    author: str
    date: str
    text: str
    reply_to_id: Optional[int]
    attachment: Optional[AttachmentSpec]
    is_service: bool


@dataclass
class TelegramExport:
    title: str
    messages: List[TelegramMessage]


def parse_telegram_archive(archive_path: Path) -> TelegramExport:
    try:
        with ZipFile(archive_path) as archive:
            result_member = _find_result_member(archive.namelist())
            if not result_member:
                raise TelegramArchiveError(_missing_result_json_message(archive.namelist()))
            payload = json.loads(archive.read(result_member).decode("utf-8-sig"))
    except TelegramArchiveError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise TelegramArchiveError(f"Не удалось прочитать архив Telegram: {exc}") from exc

    title = str(payload.get("name") or payload.get("about") or "Telegram export")
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        raise TelegramArchiveError("В result.json не найден список messages.")

    messages: List[TelegramMessage] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue

        attachment = _extract_attachment(item)
        is_service = item.get("type") == "service"
        text = _render_text(item.get("text"))
        if is_service and not text:
            text = _render_service_text(item)

        if not text and not attachment:
            continue

        messages.append(
            TelegramMessage(
                telegram_id=_safe_int(item.get("id")) or len(messages) + 1,
                author=str(item.get("from") or item.get("actor") or title or "Telegram"),
                date=str(item.get("date") or item.get("date_unixtime") or ""),
                text=text.strip(),
                reply_to_id=_safe_int(item.get("reply_to_message_id")),
                attachment=attachment,
                is_service=is_service,
            )
        )

    return TelegramExport(title=title, messages=messages)


def _find_result_member(members: List[str]) -> Optional[str]:
    for member in members:
        normalized = member.lstrip("./")
        if normalized == "result.json" or normalized.endswith("/result.json"):
            return member
    return None


def _missing_result_json_message(members: List[str]) -> str:
    normalized_members = [member.lstrip("./").lower() for member in members]
    looks_like_html_export = any(
        name == "messages.html" or name.startswith("messages") and name.endswith(".html")
        for name in normalized_members
    )
    if looks_like_html_export:
        return (
            "Неверный формат архива. Нужен Telegram Desktop export в machine-readable JSON "
            "с файлом result.json, а не HTML-export."
        )
    return "В архиве не найден result.json из Telegram Desktop export."


def _safe_int(value: object) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _render_text(raw_text: object) -> str:
    if isinstance(raw_text, str):
        return raw_text
    if not isinstance(raw_text, list):
        return ""

    chunks: List[str] = []
    for chunk in raw_text:
        if isinstance(chunk, str):
            chunks.append(chunk)
            continue
        if isinstance(chunk, dict):
            text = str(chunk.get("text") or "")
            href = chunk.get("href")
            if href and href not in text:
                chunks.append(f"{text} ({href})")
            else:
                chunks.append(text)
    return "".join(chunks)


def _render_service_text(item: Dict[str, object]) -> str:
    action = str(item.get("action") or "").strip()
    title = str(item.get("title") or "").strip()
    actor = str(item.get("actor") or item.get("from") or "Система").strip()

    if action == "create_group":
        return f"Создана группа: {title or 'без названия'}"
    if action == "edit_group_title":
        return f"Изменено название группы: {title or 'без названия'}"
    if action == "invite_members":
        return f"{actor} пригласил участников"
    if action == "remove_members":
        return f"{actor} удалил участников"
    if action == "phone_call":
        return f"Звонок: {str(item.get('discard_reason') or 'без деталей')}"

    fallback = _render_text(item.get("text"))
    if fallback:
        return fallback
    if action:
        return f"Служебное событие: {action}"
    return ""


def _extract_attachment(item: Dict[str, object]) -> Optional[AttachmentSpec]:
    for key in ("file", "photo", "thumbnail"):
        raw_path = item.get(key)
        if not isinstance(raw_path, str):
            continue
        normalized = raw_path.strip().lstrip("./")
        if not normalized or normalized.startswith("("):
            continue

        filename = Path(normalized).name
        mime_type = str(item.get("mime_type") or mimetypes.guess_type(filename)[0] or "application/octet-stream")
        return AttachmentSpec(
            archive_path=normalized,
            filename=filename,
            mime_type=mime_type,
            is_image=mime_type.startswith("image/"),
        )
    return None
