from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import AppSetting


@dataclass(frozen=True)
class RuntimeSettingDefinition:
    key: str
    default: str
    description: str
    is_secret: bool = False


SETTING_DEFINITIONS = [
    RuntimeSettingDefinition(
        key="yandex_bot_token",
        default="",
        description="OAuth-токен бота Яндекс Мессенджера для отправки сообщений.",
        is_secret=True,
    ),
    RuntimeSettingDefinition(
        key="yandex_api_base",
        default="https://botapi.messenger.yandex.net/bot/v1/",
        description="Базовый URL Bot API Яндекс Мессенджера.",
    ),
    RuntimeSettingDefinition(
        key="max_parallel_jobs",
        default="2",
        description="Максимальное число одновременных импортов, которые может выполнять worker.",
    ),
    RuntimeSettingDefinition(
        key="max_parallel_jobs_per_user",
        default="1",
        description="Максимальное число одновременных импортов для одного пользователя.",
    ),
]


def bootstrap_runtime_settings(db: Session, settings: Settings) -> None:
    defaults = {
        "yandex_bot_token": settings.bootstrap_yandex_bot_token,
        "yandex_api_base": settings.yandex_api_base,
        "max_parallel_jobs": str(settings.max_parallel_jobs),
        "max_parallel_jobs_per_user": str(settings.max_parallel_jobs_per_user),
    }

    existing = {item.key: item for item in db.execute(select(AppSetting)).scalars().all()}
    changed = False
    for definition in SETTING_DEFINITIONS:
        if definition.key in existing:
            continue
        db.add(
            AppSetting(
                key=definition.key,
                value=defaults.get(definition.key, definition.default),
                description=definition.description,
                is_secret=definition.is_secret,
            )
        )
        changed = True
    if changed:
        db.commit()


def get_runtime_settings(db: Session, settings: Settings) -> Dict[str, str]:
    bootstrap_runtime_settings(db, settings)
    rows = db.execute(select(AppSetting)).scalars().all()
    values = {row.key: row.value for row in rows}
    return {
        "yandex_bot_token": values.get("yandex_bot_token") or settings.bootstrap_yandex_bot_token,
        "yandex_api_base": values.get("yandex_api_base") or settings.yandex_api_base,
        "max_parallel_jobs": values.get("max_parallel_jobs") or str(settings.max_parallel_jobs),
        "max_parallel_jobs_per_user": values.get("max_parallel_jobs_per_user") or str(settings.max_parallel_jobs_per_user),
    }


def update_runtime_settings(db: Session, payload: Dict[str, str], *, updated_by_user_id: Optional[int]) -> None:
    rows = {item.key: item for item in db.execute(select(AppSetting)).scalars().all()}
    for definition in SETTING_DEFINITIONS:
        if definition.key not in payload:
            continue
        value = payload[definition.key].strip()
        setting = rows.get(definition.key)
        if definition.is_secret and not value and setting is not None:
            continue
        if setting is None:
            setting = AppSetting(
                key=definition.key,
                value=value,
                description=definition.description,
                is_secret=definition.is_secret,
                updated_by_user_id=updated_by_user_id,
            )
            db.add(setting)
        else:
            setting.value = value
            setting.updated_by_user_id = updated_by_user_id
    db.commit()
