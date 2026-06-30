from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import unquote

import httpx


class YandexMessengerError(Exception):
    def __init__(self, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.fatal = fatal

    def _lowered(self) -> str:
        return str(self).lower()

    def is_upload_quota_error(self) -> bool:
        lowered = self._lowered()
        return "file upload quota exceeded" in lowered or "upload quota exceeded" in lowered

    def is_file_size_error(self) -> bool:
        lowered = self._lowered()
        return "file size exceeds limit" in lowered or "file too large" in lowered

    def is_attachment_issue(self) -> bool:
        return self.is_upload_quota_error() or self.is_file_size_error()


@dataclass(frozen=True)
class SentTextResult:
    message_id: int
    thread_id: int | None = None


class YandexMessengerClient:
    def __init__(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/") + "/"
        self._json_client = httpx.Client(
            timeout=httpx.Timeout(60.0),
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._multipart_client = httpx.Client(
            timeout=httpx.Timeout(120.0),
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def create_chat(
        self,
        *,
        token: str,
        name: str,
        description: str,
        is_channel: bool,
        admins: Iterable[str],
    ) -> str:
        normalized_name = " ".join(unquote(name).split())
        if not normalized_name:
            raise YandexMessengerError("Укажите название нового чата или канала.")

        payload = {
            "name": normalized_name[:200],
            "description": description[:500],
            "channel": is_channel,
        }
        admin_items = [{"login": login.strip()} for login in admins if login.strip()]
        if admin_items:
            payload["admins"] = _unique_users(admin_items)

        data = self._post_json("chats/create/", token=token, payload=payload)
        chat_id = str(data.get("chat_id") or "").strip()
        if not chat_id:
            raise YandexMessengerError("Yandex Messenger API не вернул chat_id созданного чата.", fatal=True)
        return chat_id

    def add_members(
        self,
        *,
        token: str,
        chat_id: str,
        is_channel: bool,
        members: Iterable[str],
    ) -> None:
        member_items = [{"login": login.strip()} for login in members if login.strip()]
        if not member_items:
            return
        field_name = "subscribers" if is_channel else "members"
        payload = {
            "chat_id": chat_id,
            field_name: _unique_users(member_items),
        }
        self._post_json("chats/updateMembers/", token=token, payload=payload)

    def normalize_target_name(self, raw_target: str) -> str:
        target = " ".join(unquote(raw_target.strip()).split())
        if not target:
            raise YandexMessengerError("Укажите название нового чата или канала.")
        if len(target) > 200:
            raise YandexMessengerError("Название нового чата или канала не должно превышать 200 символов.")
        return target

    def build_chat_description(self, *, source_title: str, initiator: str) -> str:
        source = " ".join(source_title.split()).strip() or "Telegram archive"
        author = " ".join(initiator.split()).strip() or "unknown user"
        return f"Миграция из Telegram. Источник: {source}. Инициатор: {author}."[:500]

    def ensure_org_login(self, raw_login: str) -> str:
        login = raw_login.strip().lower()
        if not login:
            raise YandexMessengerError(
                "Не удалось определить логин пользователя для назначения администратором нового чата.",
                fatal=True,
            )
        if "@" not in login:
            raise YandexMessengerError(
                "Для назначения администратором нужен корпоративный логин в формате login@domain.",
                fatal=True,
            )
        return login

    def describe_target_kind(self, target_kind: str) -> str:
        return "канал" if target_kind == "channel" else "чат"

    def resolve_chat_id(self, raw_target: str) -> str:
        target = unquote(raw_target.strip())
        if not target:
            raise YandexMessengerError("Не задан chat_id.")

        match = re.search(r"/chat/#/chats/([^?#]+)", target)
        if match:
            return match.group(1).rstrip("/")

        match = re.search(r"/im/#/chats/([^?#]+)", target)
        if match:
            return match.group(1).rstrip("/")

        match = re.search(r"/#/chats/([^?#]+)", target)
        if match:
            return match.group(1).rstrip("/")

        if re.fullmatch(r"[\w\-./]+", target):
            return target.rstrip("/")

        raise YandexMessengerError("Не удалось извлечь chat_id.")

    def send_text(
        self,
        *,
        token: str,
        chat_id: str,
        text: str,
        payload_id: str,
        reply_message_id: int | None = None,
        thread_id: int | None = None,
    ) -> SentTextResult:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "payload_id": payload_id,
            "disable_web_page_preview": True,
        }
        if reply_message_id:
            payload["reply_message_id"] = reply_message_id
        if thread_id:
            payload["thread_id"] = thread_id
        data = self._post_json("messages/sendText/", token=token, payload=payload)
        message_id = int(data.get("message_id") or 0)
        resolved_thread_id = data.get("thread_id")
        return SentTextResult(
            message_id=message_id,
            thread_id=int(resolved_thread_id) if resolved_thread_id else None,
        )

    def send_file(
        self,
        *,
        token: str,
        chat_id: str,
        filename: str,
        content: bytes,
        content_type: str,
        is_image: bool,
    ) -> int:
        endpoint = "messages/sendImage/" if is_image else "messages/sendFile/"
        file_field = "image" if is_image else "document"
        data = self._post_multipart(
            endpoint,
            token=token,
            form={"chat_id": chat_id},
            files={file_field: (filename, content, content_type)},
        )
        return int(data.get("message_id") or 0)

    def _post_json(self, endpoint: str, *, token: str, payload: dict[str, Any]) -> dict[str, Any]:
        response = self._json_client.post(
            f"{self.api_base}{endpoint}",
            json=payload,
            headers={"Authorization": f"OAuth {token}", "Content-Type": "application/json"},
        )
        return self._handle_response(response)

    def _post_multipart(
        self,
        endpoint: str,
        *,
        token: str,
        form: dict[str, str],
        files: dict[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        response = self._multipart_client.post(
            f"{self.api_base}{endpoint}",
            data=form,
            files=files,
            headers={"Authorization": f"OAuth {token}"},
        )
        return self._handle_response(response)

    def _handle_response(self, response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.is_success and payload.get("ok", True):
            return payload

        description = str(payload.get("description") or response.text or "Неизвестная ошибка Yandex Messenger API")
        lowered = description.lower()
        fatal = response.status_code in {401, 403} or any(
            marker in lowered
            for marker in (
                "not a member",
                "forbidden",
                "unauthorized",
                "oauth",
                "invalid token",
                "chat not found",
                "chat does not exist",
                "specified chat does not exist",
                "user not found",
                "users do not exist",
            )
        )
        raise YandexMessengerError(description, fatal=fatal)


def _unique_users(items: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in items:
        login = item["login"].lower()
        if login in seen:
            continue
        seen.add(login)
        result.append({"login": item["login"]})
    return result
