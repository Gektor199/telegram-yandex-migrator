from __future__ import annotations

import json
import re
import secrets
from pathlib import Path


ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}

    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        match = ENV_LINE_RE.match(line)
        if not match:
            continue

        key = match.group(1)
        raw_value = line[match.end() :].strip()
        values[key] = _parse_env_value(raw_value)
    return values


def write_env_file(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    applied_keys: set[str] = set()
    rendered_lines: list[str] = []

    for line in existing_lines:
        match = ENV_LINE_RE.match(line)
        if not match:
            rendered_lines.append(line)
            continue

        key = match.group(1)
        if key not in updates:
            rendered_lines.append(line)
            continue

        rendered_lines.append(f"{key}={_format_env_value(updates[key])}")
        applied_keys.add(key)

    for key, value in updates.items():
        if key in applied_keys:
            continue
        rendered_lines.append(f"{key}={_format_env_value(value)}")

    output = "\n".join(rendered_lines).rstrip() + "\n"
    path.write_text(output, encoding="utf-8")


def ensure_local_admin_credentials(path: Path) -> dict[str, str]:
    values = read_env_file(path)
    updates: dict[str, str] = {}

    if not values.get("LOCAL_ADMIN_LOGIN"):
        updates["LOCAL_ADMIN_LOGIN"] = "admin"
    if not values.get("LOCAL_ADMIN_PASSWORD"):
        updates["LOCAL_ADMIN_PASSWORD"] = secrets.token_urlsafe(16)

    if updates:
        write_env_file(path, updates)
        values.update(updates)
    return values


def _parse_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        try:
            return json.loads(value) if value[0] == '"' else value[1:-1]
        except json.JSONDecodeError:
            return value[1:-1]
    return value


def _format_env_value(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)
