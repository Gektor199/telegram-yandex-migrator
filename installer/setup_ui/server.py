from __future__ import annotations

import html
import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from app.env_store import read_env_file, write_env_file


ENV_PATH = Path(os.environ.get("SETUP_ENV_FILE", "/workspace/.env")).resolve()
COMPLETE_FLAG = Path(os.environ.get("SETUP_COMPLETE_FLAG", "/workspace/.setup-complete")).resolve()
PORT = int(os.environ.get("SETUP_PORT", "8787"))


BOOL_TRUE = {"1", "true", "yes", "on"}

FIELDS = [
    ("PUBLIC_BASE_URL", "Public base URL", "Например, https://migrator.example.com", True),
    ("YANDEX_BOT_TOKEN", "Yandex bot token", "Токен бота Яндекс Мессенджера", True),
    ("YANDEX_DISK_ENABLED", "Использовать Яндекс Диск", "", False),
    ("YANDEX_DISK_OAUTH_TOKEN", "Yandex Disk OAuth token", "Нужен, если включён Яндекс Диск", False),
    ("YANDEX_DISK_ROOT_REFERENCE", "Yandex Disk root reference", "Например, disk:/Migrator Files", False),
    ("YANDEX_DISK_ORG_ID", "Yandex Disk org id", "Необязательно", False),
    ("SSO_ENABLED", "Включить SSO", "", False),
    ("OIDC_SERVER_METADATA_URL", "OIDC metadata URL", "https://.../.well-known/openid-configuration", False),
    ("OIDC_CLIENT_ID", "OIDC client id", "", False),
    ("OIDC_CLIENT_SECRET", "OIDC client secret", "", False),
    ("OIDC_SCOPE", "OIDC scope", "Обычно: openid profile email", False),
    ("ADMIN_EMAILS", "Admin emails", "Через запятую", False),
    ("ADMIN_DOMAINS", "Admin domains", "Через запятую", False),
    ("HTTP_PORT", "HTTP port", "Порт локального web-контейнера, например 8081", True),
    ("MAX_ARCHIVE_SIZE_MB", "Max archive size (MB)", "Например, 40960", True),
]

READONLY_FIELDS = [
    ("LOCAL_ADMIN_LOGIN", "Local admin login"),
    ("LOCAL_ADMIN_PASSWORD", "Local admin password"),
    ("POSTGRES_DB", "Postgres DB"),
    ("POSTGRES_USER", "Postgres user"),
    ("POSTGRES_PASSWORD", "Postgres password"),
]


def _is_enabled(value: str) -> bool:
    return value.strip().lower() in BOOL_TRUE


def _normalize_updates(form: dict[str, str], current: dict[str, str]) -> dict[str, str]:
    updates = dict(current)
    for key, _, _, _ in FIELDS:
        raw = form.get(key, "")
        if key in {"YANDEX_DISK_ENABLED", "SSO_ENABLED"}:
            updates[key] = "true" if raw == "on" else "false"
        else:
            updates[key] = raw.strip()
    return updates


def _validate(values: dict[str, str]) -> list[str]:
    errors: list[str] = []
    if not values.get("PUBLIC_BASE_URL", "").strip():
        errors.append("Укажите внешний URL сервиса.")
    if not values.get("YANDEX_BOT_TOKEN", "").strip():
        errors.append("Укажите Yandex bot token.")
    if not values.get("HTTP_PORT", "").strip().isdigit():
        errors.append("HTTP port должен быть числом.")
    if not values.get("MAX_ARCHIVE_SIZE_MB", "").strip().isdigit():
        errors.append("Max archive size должен быть числом.")
    if _is_enabled(values.get("SSO_ENABLED", "")):
        for key in ("OIDC_SERVER_METADATA_URL", "OIDC_CLIENT_ID", "OIDC_CLIENT_SECRET"):
            if not values.get(key, "").strip():
                errors.append(f"Заполните {key} для включённого SSO.")
    if _is_enabled(values.get("YANDEX_DISK_ENABLED", "")):
        for key in ("YANDEX_DISK_OAUTH_TOKEN", "YANDEX_DISK_ROOT_REFERENCE"):
            if not values.get(key, "").strip():
                errors.append(f"Заполните {key} для включённого Яндекс Диска.")
    return errors


def _render_page(values: dict[str, str], errors: list[str], saved: bool) -> bytes:
    def field_html(key: str, label: str, hint: str, required: bool) -> str:
        value = values.get(key, "")
        if key in {"YANDEX_DISK_ENABLED", "SSO_ENABLED"}:
            checked = " checked" if _is_enabled(value) else ""
            return (
                f'<label class="toggle"><input type="checkbox" name="{html.escape(key)}"{checked}>'
                f"<span>{html.escape(label)}</span></label>"
            )

        escaped = html.escape(value)
        input_type = "password" if "SECRET" in key or key.endswith("TOKEN") else "text"
        req = " required" if required else ""
        hint_html = f'<div class="hint">{html.escape(hint)}</div>' if hint else ""
        return (
            f'<label><span>{html.escape(label)}</span>'
            f'<input type="{input_type}" name="{html.escape(key)}" value="{escaped}"{req}>'
            f"{hint_html}</label>"
        )

    readonly_html = "".join(
        f'<label><span>{html.escape(label)}</span><input type="text" value="{html.escape(values.get(key, ""))}" readonly></label>'
        for key, label in READONLY_FIELDS
    )
    fields_html = "".join(field_html(*field) for field in FIELDS)
    errors_html = (
        '<div class="errors">' + "".join(f"<p>{html.escape(item)}</p>" for item in errors) + "</div>"
        if errors
        else ""
    )
    saved_html = '<div class="saved">Конфигурация сохранена. Основной стек можно запускать.</div>' if saved else ""

    page = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Migrator Setup</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f4ef; color: #171717; }}
    .shell {{ max-width: 980px; margin: 40px auto; padding: 0 20px; }}
    .card {{ background: #fff; border: 1px solid #e7e1d5; border-radius: 24px; padding: 28px; box-shadow: 0 24px 60px rgba(40, 28, 5, 0.08); }}
    h1 {{ margin: 0 0 8px; font-size: 38px; }}
    p.lead {{ margin: 0 0 24px; color: #6c655a; }}
    h2 {{ margin: 28px 0 12px; font-size: 20px; }}
    form {{ display: grid; gap: 16px; }}
    label {{ display: grid; gap: 6px; }}
    span {{ font-weight: 600; }}
    input {{ border: 1px solid #d8d1c4; border-radius: 14px; padding: 14px 16px; font-size: 16px; }}
    input[readonly] {{ background: #f7f7f5; color: #5c5c58; }}
    .hint {{ color: #7a746a; font-size: 13px; }}
    .toggle {{ display: flex; align-items: center; gap: 12px; }}
    .toggle input {{ width: 18px; height: 18px; padding: 0; }}
    .grid {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 16px; }}
    .errors {{ background: #fff4f3; border: 1px solid #f3b7b3; color: #8f2318; border-radius: 16px; padding: 16px; }}
    .errors p {{ margin: 0 0 6px; }}
    .errors p:last-child {{ margin-bottom: 0; }}
    .saved {{ background: #edf9ef; border: 1px solid #9fd4a8; color: #216a32; border-radius: 16px; padding: 16px; }}
    button {{ border: 0; border-radius: 999px; padding: 16px 24px; background: #171717; color: #fff; font-size: 16px; cursor: pointer; }}
    @media (max-width: 800px) {{ .grid {{ grid-template-columns: 1fr; }} h1 {{ font-size: 30px; }} }}
  </style>
</head>
<body>
  <main class="shell">
    <section class="card">
      <h1>Настройка Migrator</h1>
      <p class="lead">Заполните обязательные реквизиты. После сохранения установщик запустит основной стек автоматически.</p>
      {errors_html}
      {saved_html}
      <h2>Сгенерированные учётные данные</h2>
      <div class="grid">{readonly_html}</div>
      <h2>Параметры сервиса</h2>
      <form method="post" action="/save">
        <div class="grid">{fields_html}</div>
        <button type="submit">Сохранить и продолжить</button>
      </form>
    </section>
  </main>
</body>
</html>"""
    return page.encode("utf-8")


class SetupHandler(BaseHTTPRequestHandler):
    def _load_values(self) -> dict[str, str]:
        return read_env_file(ENV_PATH)

    def do_GET(self) -> None:  # noqa: N802
        values = self._load_values()
        payload = _render_page(values, errors=[], saved=COMPLETE_FLAG.exists())
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/save":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length).decode("utf-8")
        form = {key: values[-1] for key, values in parse_qs(body, keep_blank_values=True).items()}
        current = self._load_values()
        updates = _normalize_updates(form, current)
        errors = _validate(updates)
        if errors:
            payload = _render_page(updates, errors=errors, saved=False)
            self.send_response(HTTPStatus.BAD_REQUEST)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        write_env_file(ENV_PATH, updates)
        COMPLETE_FLAG.write_text(json.dumps({"saved": True}, ensure_ascii=False), encoding="utf-8")
        payload = _render_page(updates, errors=[], saved=True)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args) -> None:  # noqa: A003
        return


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", PORT), SetupHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
