#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALLER_DIR="${PROJECT_DIR}/installer"
ENV_FILE="${PROJECT_DIR}/.env"
ENV_TEMPLATE="${INSTALLER_DIR}/.env.template"
SETUP_FLAG="${PROJECT_DIR}/.setup-complete"
INSTALLER_PORT="${INSTALLER_PORT:-8787}"

require_linux() {
  if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Этот установщик рассчитан на Linux." >&2
    exit 1
  fi
}

command_exists() {
  command -v "$1" >/dev/null 2>&1
}

run_python() {
  local script="$1"
  shift
  if command_exists python3; then
    python3 - "$@" <<<"$script"
    return
  fi
  docker run --rm -i -v "${PROJECT_DIR}:/workspace" -w /workspace python:3.12-slim python - "$@" <<<"$script"
}

gen_secret() {
  tr -dc 'A-Za-z0-9' </dev/urandom | head -c 48
}

ensure_docker() {
  if command_exists docker; then
    return
  fi

  echo "Docker не найден."
  read -r -p "Установить Docker автоматически? [y/N] " reply
  if [[ ! "$reply" =~ ^[Yy]$ ]]; then
    echo "Установка прервана: Docker обязателен." >&2
    exit 1
  fi

  if command_exists apt-get; then
    curl -fsSL https://get.docker.com | sudo sh
  else
    echo "Автоустановка Docker поддерживается только на Debian/Ubuntu-подобных системах." >&2
    echo "Установите Docker вручную и запустите install.sh снова." >&2
    exit 1
  fi
}

ensure_compose() {
  if docker compose version >/dev/null 2>&1; then
    return
  fi
  echo "Docker Compose plugin не найден. Обновите Docker до версии с docker compose." >&2
  exit 1
}

ensure_env_file() {
  if [[ ! -f "$ENV_FILE" ]]; then
    cp "$ENV_TEMPLATE" "$ENV_FILE"
  fi
}

set_env_value() {
  local key="$1"
  local value="$2"
  run_python "$(cat <<'PY'
from pathlib import Path
import json
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
prefix = f"{key}="
rendered = f"{key}={json.dumps(value, ensure_ascii=False)}"
updated = False
for idx, line in enumerate(lines):
    if line.startswith(prefix):
        lines[idx] = rendered
        updated = True
        break
if not updated:
    lines.append(rendered)
path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
PY
)" "$ENV_FILE" "$key" "$value"
}

bootstrap_env() {
  ensure_env_file

  local secret_key db_name db_user db_password admin_login admin_password
  secret_key="migrator_$(gen_secret)"
  db_name="migrator_$(tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
  db_user="migrator_$(tr -dc 'a-z0-9' </dev/urandom | head -c 8)"
  db_password="$(gen_secret)"
  admin_login="admin"
  admin_password="$(gen_secret)"

  grep -q '^SECRET_KEY=' "$ENV_FILE" || set_env_value "SECRET_KEY" "$secret_key"
  grep -q '^POSTGRES_DB=' "$ENV_FILE" || set_env_value "POSTGRES_DB" "$db_name"
  grep -q '^POSTGRES_USER=' "$ENV_FILE" || set_env_value "POSTGRES_USER" "$db_user"
  grep -q '^POSTGRES_PASSWORD=' "$ENV_FILE" || set_env_value "POSTGRES_PASSWORD" "$db_password"
  grep -q '^LOCAL_ADMIN_LOGIN=' "$ENV_FILE" || set_env_value "LOCAL_ADMIN_LOGIN" "$admin_login"
  grep -q '^LOCAL_ADMIN_PASSWORD=' "$ENV_FILE" || set_env_value "LOCAL_ADMIN_PASSWORD" "$admin_password"

  run_python "$(cat <<'PY'
from pathlib import Path
from app.env_store import read_env_file, write_env_file
import sys
path = Path(sys.argv[1])
values = read_env_file(path)
write_env_file(path, {
    "DATABASE_URL": f"postgresql+psycopg://{values['POSTGRES_USER']}:{values['POSTGRES_PASSWORD']}@db:5432/{values['POSTGRES_DB']}"
})
PY
)" "$ENV_FILE"
}

start_setup_ui() {
  rm -f "$SETUP_FLAG"
  export INSTALLER_PORT
  docker compose -f "${INSTALLER_DIR}/docker-compose.setup.yml" up -d
  local url="http://127.0.0.1:${INSTALLER_PORT}"
  echo
  echo "Откройте страницу настройки: ${url}"
  echo "После сохранения конфигурации установщик продолжит запуск автоматически."
  echo
  if command_exists xdg-open; then
    xdg-open "$url" >/dev/null 2>&1 || true
  fi
}

wait_for_setup_completion() {
  trap 'docker compose -f "${INSTALLER_DIR}/docker-compose.setup.yml" down >/dev/null 2>&1 || true' EXIT
  while [[ ! -f "$SETUP_FLAG" ]]; do
    sleep 2
  done
  docker compose -f "${INSTALLER_DIR}/docker-compose.setup.yml" down
  rm -f "$SETUP_FLAG"
  trap - EXIT
}

start_stack() {
  cd "$PROJECT_DIR"
  docker compose up -d --build
  echo
  echo "Сервисы запущены."
  echo "Проверьте health:"
  echo "  curl http://127.0.0.1:\${HTTP_PORT:-8081}/health"
}

main() {
  require_linux
  ensure_docker
  ensure_compose
  bootstrap_env
  start_setup_ui
  wait_for_setup_completion
  start_stack
}

main "$@"
