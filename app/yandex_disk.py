from __future__ import annotations

import re
import time
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Optional

import httpx


class YandexDiskError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        error_code: str = "",
        request_id: str = "",
        trace_id: str = "",
        request_method: str = "",
        request_url: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.request_id = request_id
        self.trace_id = trace_id
        self.request_method = request_method
        self.request_url = request_url

    def is_locked(self) -> bool:
        lowered = str(self).lower()
        return "resource is locked" in lowered or "locked" in lowered

    def is_insufficient_storage(self) -> bool:
        lowered = str(self).lower()
        error_code = self.error_code.lower()
        return (
            self.status_code == 507
            or "insufficient storage" in lowered
            or "not enough space" in lowered
            or "quota exceeded" in lowered
            or "insufficientstorage" in error_code
        )


@dataclass(frozen=True)
class UploadedDiskFile:
    path: str
    public_url: str


class YandexDiskClient:
    _LOCK_RETRY_DELAYS = (0.5, 1.0, 2.0, 4.0)
    _UPLOAD_RETRY_DELAYS = (0.0, 1.0, 2.0, 4.0)

    def __init__(self, api_base: str, root_reference: str) -> None:
        self.api_base = api_base.rstrip("/")
        self.root_reference = self._normalize_root_reference(root_reference)
        transport = httpx.HTTPTransport(local_address="0.0.0.0")
        self._api_client = httpx.Client(
            timeout=httpx.Timeout(120.0),
            transport=transport,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
        self._upload_client = httpx.Client(
            timeout=httpx.Timeout(connect=60.0, write=1800.0, read=600.0, pool=60.0),
            transport=httpx.HTTPTransport(local_address="0.0.0.0"),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )

    def build_chat_folder_path(self, chat_name: str) -> str:
        safe_name = self.sanitize_segment(chat_name) or "telegram-migration"
        return f"{self.root_reference}/{safe_name}"

    def upload_public_file(
        self,
        *,
        token: str,
        chat_folder_path: str,
        relative_archive_path: str,
        content: bytes,
        content_type: str,
        org_id: str = "",
    ) -> UploadedDiskFile:
        return self.upload_public_file_stream(
            token=token,
            chat_folder_path=chat_folder_path,
            relative_archive_path=relative_archive_path,
            stream_factory=lambda: BytesIO(content),
            expected_size=len(content),
            content_type=content_type,
            org_id=org_id,
        )

    def upload_public_file_stream(
        self,
        *,
        token: str,
        chat_folder_path: str,
        relative_archive_path: str,
        stream_factory: Callable[[], BinaryIO],
        expected_size: int,
        content_type: str,
        org_id: str = "",
    ) -> UploadedDiskFile:
        relative_path = self._normalize_relative_path(relative_archive_path)
        target_path = f"{chat_folder_path}/{relative_path}"
        self._ensure_parent_folders(token=token, path=target_path)
        resource: dict[str, Any] = {}
        last_error: Optional[Exception] = None
        for delay in self._UPLOAD_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            upload_href = self._get_upload_href(token=token, path=target_path)
            try:
                self._upload_stream(
                    upload_href=upload_href,
                    stream_factory=stream_factory,
                    content_length=expected_size,
                    content_type=content_type,
                )
                resource = self._wait_for_resource(token=token, path=target_path)
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                resource = self._wait_for_stable_resource(
                    token=token,
                    path=target_path,
                    expected_size=expected_size,
                    quiet_window_seconds=7.0,
                )
                if self._resource_size(resource) >= expected_size:
                    break
                continue

            if self._resource_size(resource) >= expected_size:
                break
            resource = self._wait_for_stable_resource(
                token=token,
                path=target_path,
                expected_size=expected_size,
                quiet_window_seconds=7.0,
            )
            if self._resource_size(resource) >= expected_size:
                break
        else:
            observed_size = self._resource_size(resource)
            if last_error:
                raise YandexDiskError(
                    "Загрузка файла на Яндекс Диск не завершилась. "
                    "Попробуйте повторить импорт или используйте Retry для ссылок."
                ) from last_error
            raise YandexDiskError(
                "Файл на Яндекс Диске не догрузился полностью. "
                f"Ожидалось {expected_size} байт, получено {observed_size}."
            )

        if self._resource_size(resource) < expected_size:
            observed_size = self._resource_size(resource)
            raise YandexDiskError(
                "Файл на Яндекс Диске не догрузился полностью. "
                f"Ожидалось {expected_size} байт, получено {observed_size}."
            )
        existing_public_url = str(resource.get("public_url") or resource.get("public_key") or "").strip()
        if existing_public_url:
            return UploadedDiskFile(path=target_path, public_url=existing_public_url)
        self._publish_resource(token=token, path=target_path, org_id=org_id)
        resource = self._wait_for_public_resource(token=token, path=target_path)
        public_url = str(resource.get("public_url") or resource.get("public_key") or "").strip()
        if not public_url:
            raise YandexDiskError("Яндекс Диск не вернул публичную ссылку на загруженный файл.")
        return UploadedDiskFile(path=target_path, public_url=public_url)

    def probe(self, *, token: str, folder_name: str = "telegram-migrator-probe", org_id: str = "") -> UploadedDiskFile:
        probe_folder = self.build_chat_folder_path(folder_name)
        content = b"Telegram Yandex migration probe\n"
        return self.upload_public_file(
            token=token,
            chat_folder_path=probe_folder,
            relative_archive_path="probe.txt",
            content=content,
            content_type="text/plain; charset=utf-8",
            org_id=org_id,
        )

    def _ensure_parent_folders(self, *, token: str, path: str) -> None:
        parent = PurePosixPath(path).parent
        if str(parent) in {".", "/", ""}:
            return

        current = ""
        parts = str(parent).split("/")
        if not parts:
            return

        if ":" in parts[0]:
            current = parts[0]
            start_index = 1
        else:
            start_index = 0

        for part in parts[start_index:]:
            if not part:
                continue
            current = f"{current}/{part}" if current else part
            self._create_folder(token=token, path=current)

    def _create_folder(self, *, token: str, path: str) -> None:
        def operation() -> None:
            response = self._request("PUT", self._resources_endpoint(path), token=token, params={"path": path})
            if response.status_code in {201, 409}:
                return
            self._raise_response_error(response, fallback="Не удалось создать папку на Яндекс Диске.")

        self._retry_locked(operation, fallback="Не удалось создать папку на Яндекс Диске.")

    def _get_upload_href(self, *, token: str, path: str) -> str:
        def operation() -> str:
            response = self._request(
                "GET",
                self._upload_endpoint(path),
                token=token,
                params={"path": path, "overwrite": "true"},
            )
            payload = self._parse_json(response)
            href = str(payload.get("href") or "").strip()
            if not href:
                raise YandexDiskError("Яндекс Диск не вернул ссылку для загрузки файла.")
            return href

        return self._retry_locked(operation, fallback="Яндекс Диск не вернул ссылку для загрузки файла.")

    def _upload_stream(
        self,
        *,
        upload_href: str,
        stream_factory: Callable[[], BinaryIO],
        content_length: int,
        content_type: str,
    ) -> None:
        last_error: Optional[Exception] = None
        for delay in self._UPLOAD_RETRY_DELAYS:
            if delay:
                time.sleep(delay)
            try:
                with stream_factory() as stream:
                    response = self._upload_client.put(
                        upload_href,
                        content=self._iter_stream_chunks(stream),
                        headers={
                            "Content-Type": content_type,
                            "Content-Length": str(max(content_length, 0)),
                        },
                    )
                if response.status_code in {201, 202}:
                    return
                self._raise_response_error(response, fallback="Не удалось загрузить файл на Яндекс Диск.")
            except YandexDiskError as exc:
                last_error = exc
                if exc.is_locked():
                    continue
                raise
            except httpx.TimeoutException as exc:
                last_error = exc
                continue
            except httpx.TransportError as exc:
                last_error = exc
                continue
        if last_error:
            raise YandexDiskError("Не удалось загрузить файл на Яндекс Диск.") from last_error
        raise YandexDiskError("Не удалось загрузить файл на Яндекс Диск.")

    @staticmethod
    def _iter_stream_chunks(stream: BinaryIO, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        while True:
            chunk = stream.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def _publish_resource(self, *, token: str, path: str, org_id: str = "") -> None:
        publish_path = self._publish_path(path)
        params = {"path": publish_path}
        json_payload: dict[str, Any] | None = None
        if org_id:
            params["allow_address_access"] = "true"
            json_payload = {
                "public_settings": {
                    "accesses": [
                        {
                            "type": "macro",
                            "id": 0,
                            "macros": ["employees"],
                            "org_id": int(org_id),
                            "rights": ["read"],
                        }
                    ]
                }
            }
        last_error: Optional[YandexDiskError] = None
        for attempt in range(4):
            try:
                def operation() -> None:
                    response = self._request("PUT", "/resources/publish", token=token, params=params, json=json_payload)
                    if response.status_code in {200, 201, 202, 409}:
                        return
                    self._raise_response_error(
                        response,
                        fallback=f"Не удалось опубликовать файл на Яндекс Диске. source_path={path} publish_path={publish_path}",
                    )

                self._retry_locked(
                    operation,
                    fallback=f"Не удалось опубликовать файл на Яндекс Диске. source_path={path} publish_path={publish_path}",
                )
                return
            except YandexDiskError as exc:
                last_error = exc
                if "not found" not in str(exc).lower() and "resource not found" not in str(exc).lower():
                    raise
                time.sleep(0.4 * (attempt + 1))
        raise last_error or YandexDiskError(
            f"Не удалось опубликовать файл на Яндекс Диске. source_path={path} publish_path={publish_path}"
        )

    def _get_resource(self, *, token: str, path: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            response = self._request(
                "GET",
                self._resources_endpoint(path),
                token=token,
                params={"path": path, "fields": "name,path,size,public_url,public_key"},
            )
            return self._parse_json(response)

        return self._retry_locked(operation, fallback=f"Не удалось прочитать ресурс на Яндекс Диске. path={path}")

    def _get_resource_or_none(self, *, token: str, path: str) -> dict[str, Any]:
        def operation() -> dict[str, Any]:
            response = self._request(
                "GET",
                self._resources_endpoint(path),
                token=token,
                params={"path": path, "fields": "name,path,size,public_url,public_key"},
            )
            if response.status_code == 404:
                return {}
            return self._parse_json(response)

        return self._retry_locked(operation, fallback=f"Не удалось прочитать ресурс на Яндекс Диске. path={path}")

    def _wait_for_resource(self, *, token: str, path: str) -> dict[str, Any]:
        last_error: Optional[YandexDiskError] = None
        for attempt in range(5):
            try:
                return self._get_resource(token=token, path=path)
            except YandexDiskError as exc:
                last_error = exc
                time.sleep(0.35 * (attempt + 1))
        raise last_error or YandexDiskError(
            f"Яндекс Диск не подтвердил создание ресурса после загрузки файла. path={path}"
        )

    def _wait_for_public_resource(self, *, token: str, path: str) -> dict[str, Any]:
        last_resource: dict[str, Any] = {}
        for attempt in range(5):
            resource = self._wait_for_resource(token=token, path=path)
            if str(resource.get("public_url") or resource.get("public_key") or "").strip():
                return resource
            last_resource = resource
            time.sleep(0.35 * (attempt + 1))
        return last_resource

    def _wait_for_stable_resource(
        self,
        *,
        token: str,
        path: str,
        expected_size: int,
        quiet_window_seconds: float,
    ) -> dict[str, Any]:
        last_resource: dict[str, Any] = {}
        last_size: Optional[int] = None
        last_size_change_at = time.monotonic()
        deadline = time.monotonic() + max(quiet_window_seconds + 2.0, 12.0)

        while time.monotonic() < deadline:
            resource = self._get_resource_or_none(token=token, path=path)
            current_size = self._resource_size(resource)

            if resource:
                last_resource = resource
            if current_size >= expected_size > 0:
                return resource
            if last_size is None or current_size != last_size:
                last_size = current_size
                last_size_change_at = time.monotonic()
            elif time.monotonic() - last_size_change_at >= quiet_window_seconds:
                return resource
            time.sleep(1.0)

        return last_resource

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        token: str,
        params: Optional[dict[str, str]] = None,
        json: Optional[dict[str, Any]] = None,
    ) -> httpx.Response:
        return self._api_client.request(
            method,
            f"{self.api_base}{endpoint}",
            params=params,
            json=json,
            headers={"Authorization": f"OAuth {token}"},
        )

    def _resources_endpoint(self, path: str) -> str:
        return "/virtual-disks/resources" if self._is_shared_disk_path(path) else "/resources"

    def _upload_endpoint(self, path: str) -> str:
        return "/virtual-disks/resources/upload" if self._is_shared_disk_path(path) else "/resources/upload"

    @staticmethod
    def _is_shared_disk_path(path: str) -> bool:
        return path.startswith("vd:")

    def _publish_path(self, path: str) -> str:
        if not self._is_shared_disk_path(path):
            return path

        match = re.match(r"^vd:([^:]+):disk:(/.*)?$", path)
        if not match:
            return path

        vd_hash = match.group(1)
        suffix = (match.group(2) or "").strip()
        suffix = "/" + suffix.lstrip("/") if suffix else ""
        return f"/vd/{vd_hash}/disk{suffix}"

    def _parse_json(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_success:
            try:
                return response.json()
            except ValueError as exc:  # noqa: BLE001
                raise YandexDiskError(f"Яндекс Диск вернул невалидный JSON: {exc}") from exc
        self._raise_response_error(response, fallback="Неизвестная ошибка Яндекс Диска.")
        return {}

    def _raise_response_error(self, response: httpx.Response, *, fallback: str) -> None:
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        error_code = str(payload.get("error") or "").strip()
        description = str(payload.get("description") or payload.get("message") or response.text or fallback).strip()
        request_id = (
            response.headers.get("X-Request-Id")
            or response.headers.get("X-Request-ID")
            or response.headers.get("X-Ya-Request-Id")
            or response.headers.get("X-Yandex-Request-Id")
            or response.headers.get("X-RequestId")
            or ""
        ).strip()
        trace_id = (
            response.headers.get("Trace-Id")
            or response.headers.get("X-Trace-Id")
            or response.headers.get("traceparent")
            or ""
        ).strip()
        request_method = response.request.method if response.request else ""
        request_url = str(response.request.url) if response.request else ""
        trace_parts = [f"status={response.status_code}"]
        if error_code:
            trace_parts.append(f"error_code={error_code}")
        if request_id:
            trace_parts.append(f"request_id={request_id}")
        if trace_id:
            trace_parts.append(f"trace_id={trace_id}")
        if request_method:
            trace_parts.append(f"method={request_method}")
        if request_url:
            trace_parts.append(f"url={request_url}")
        trace_suffix = f" [{' '.join(trace_parts)}]" if trace_parts else ""
        raise YandexDiskError(
            f"{description or fallback}{trace_suffix}",
            status_code=response.status_code,
            error_code=error_code,
            request_id=request_id,
            trace_id=trace_id,
            request_method=request_method,
            request_url=request_url,
        )

    def _retry_locked(self, operation, *, fallback: str):
        last_error: Optional[YandexDiskError] = None
        for attempt, delay in enumerate((0.0, *self._LOCK_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except YandexDiskError as exc:
                last_error = exc
                if not exc.is_locked():
                    raise
                if attempt == len(self._LOCK_RETRY_DELAYS):
                    break
        raise last_error or YandexDiskError(fallback)

    @staticmethod
    def _resource_size(resource: Optional[dict[str, Any]]) -> int:
        if not resource:
            return 0
        try:
            return int(resource.get("size") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def sanitize_segment(value: str) -> str:
        cleaned = re.sub(r"[<>:\"\\\\|?*]+", " ", value).strip().rstrip(".")
        cleaned = re.sub(r"\s+", " ", cleaned)
        return cleaned[:120]

    def _normalize_root_reference(self, value: str) -> str:
        raw = value.strip().strip("/")
        if not raw:
            return "disk:/telegram-migrator"
        if ":" in raw:
            return raw.rstrip("/")
        return f"disk:/{raw}"

    def _normalize_relative_path(self, value: str) -> str:
        raw = value.strip().replace("\\", "/").lstrip("./")
        parts = [self.sanitize_segment(part) for part in raw.split("/") if part not in {"", ".", ".."}]
        parts = [part for part in parts if part]
        if not parts:
            return "unnamed-file"
        return str(PurePosixPath(*parts))
