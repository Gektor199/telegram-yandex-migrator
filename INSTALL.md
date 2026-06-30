# Установка Migrator на новый Linux-сервер

## Что получится

После установки поднимутся:

- `web` — FastAPI приложение
- `worker` — обработчик очереди импортов
- `db` — PostgreSQL

Установка рассчитана на сценарий:

1. передать один архив на сервер,
2. распаковать его,
3. запустить `install.sh`,
4. заполнить форму настройки в браузере,
5. дождаться запуска `docker compose`.

## Что требуется от сервера

- Linux
- исходящий доступ в интернет
- `curl`
- права на запуск `docker` или `sudo`

Если Docker отсутствует, `install.sh` предложит установить его автоматически на Debian/Ubuntu-подобной системе.

## Быстрый сценарий

```bash
tar -xzf migrator-installer-<version>.tar.gz
cd migrator
bash install.sh
```

После запуска:

1. установщик проверит `docker` и `docker compose`;
2. создаст `.env` и сгенерирует:
   - `SECRET_KEY`
   - `POSTGRES_DB`
   - `POSTGRES_USER`
   - `POSTGRES_PASSWORD`
   - `LOCAL_ADMIN_LOGIN`
   - `LOCAL_ADMIN_PASSWORD`
3. поднимет временный setup UI на `http://127.0.0.1:8787`;
4. после сохранения формы остановит setup UI и запустит основной стек.

## Что нужно заполнить в setup UI

Минимально:

- `PUBLIC_BASE_URL`
- `YANDEX_BOT_TOKEN`

Если нужен Яндекс Диск:

- включить `YANDEX_DISK_ENABLED`
- указать `YANDEX_DISK_OAUTH_TOKEN`
- указать `YANDEX_DISK_ROOT_REFERENCE`

Если нужен SSO:

- включить `SSO_ENABLED`
- заполнить:
  - `OIDC_SERVER_METADATA_URL`
  - `OIDC_CLIENT_ID`
  - `OIDC_CLIENT_SECRET`

Дополнительно:

- `ADMIN_EMAILS`
- `ADMIN_DOMAINS`
- `HTTP_PORT`
- `MAX_ARCHIVE_SIZE_MB`

## Где лежат данные

- конфигурация: `.env`
- загруженные архивы и runtime-данные: docker volume `app_data`
- база данных: docker volume `postgres_data`

## Проверка после установки

```bash
docker compose ps
curl http://127.0.0.1:${HTTP_PORT:-8081}/health
```

Ожидаемо:

```json
{"status":"ok","maintenance_mode":false}
```

## Обновление установленного сервиса

1. заменить код новой версией архива;
2. при необходимости обновить `.env`;
3. выполнить:

```bash
docker compose up -d --build
```

## Сборка инсталляционного архива

Из рабочей копии проекта:

```bash
bash installer/build-installer.sh
```

Скрипт положит готовый архив в `dist/`.
