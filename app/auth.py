from __future__ import annotations

from datetime import datetime, timezone
from secrets import compare_digest
from typing import Optional

from authlib.integrations.starlette_client import OAuth
from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.models import User


def get_oauth(settings: Optional[Settings] = None) -> OAuth:
    settings = settings or get_settings()
    oauth = OAuth()
    if settings.sso_enabled and settings.oidc_server_metadata_url and settings.oidc_client_id and settings.oidc_client_secret:
        oauth.register(
            name="oidc",
            server_metadata_url=settings.oidc_server_metadata_url,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            client_kwargs={"scope": settings.oidc_scope},
        )
    return oauth


def sso_configured(settings: Settings) -> bool:
    return bool(
        settings.sso_enabled
        and settings.oidc_server_metadata_url
        and settings.oidc_client_id
        and settings.oidc_client_secret
    )


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Требуется авторизация.")

    user = db.get(User, user_id)
    if not user:
        request.session.clear()
        raise HTTPException(status_code=401, detail="Сессия недействительна.")
    return user


def get_optional_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get(User, user_id)
    if not user:
        request.session.clear()
    return user


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Доступ только для администратора.")
    return user


def authenticate_local_admin(*, login: str, password: str, settings: Settings) -> bool:
    return bool(
        settings.local_admin_login
        and settings.local_admin_password
        and compare_digest(login, settings.local_admin_login)
        and compare_digest(password, settings.local_admin_password)
    )


def upsert_local_admin(db: Session, settings: Settings) -> User:
    user = db.execute(select(User).where(User.external_subject == "local-admin")).scalar_one_or_none()
    email = f"{settings.local_admin_login}@local.admin"

    if user is None:
        user = User(
            external_subject="local-admin",
            email=email,
            full_name="Local administrator",
            is_admin=True,
        )
        db.add(user)
    else:
        user.email = email
        user.full_name = "Local administrator"
        user.is_admin = True

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user


def is_sso_admin(email: str, settings: Settings) -> bool:
    normalized_email = email.strip().lower()
    if not normalized_email:
        return False

    if normalized_email in {item.lower() for item in settings.admin_emails}:
        return True

    if "@" not in normalized_email:
        return False

    domain = normalized_email.split("@", 1)[1]
    return domain in {item.lower() for item in settings.admin_domains}


def sync_sso_admin_flags(db: Session, settings: Settings) -> None:
    users = db.execute(select(User).where(User.external_subject != "local-admin")).scalars().all()
    changed = False
    for user in users:
        expected = is_sso_admin(user.email, settings)
        if user.is_admin != expected:
            user.is_admin = expected
            changed = True
    if changed:
        db.commit()


def upsert_user_from_oidc(db: Session, userinfo: dict, settings: Settings) -> User:
    subject = str(userinfo.get("sub") or "").strip()
    email = str(userinfo.get("email") or "").strip().lower()
    full_name = str(userinfo.get("name") or userinfo.get("preferred_username") or email or subject).strip()

    if not subject or not email:
        raise HTTPException(status_code=400, detail="SSO-провайдер не вернул обязательные поля sub/email.")

    user = db.execute(select(User).where(User.external_subject == subject)).scalar_one_or_none()
    is_admin = is_sso_admin(email, settings)
    if user is None:
        user = User(
            external_subject=subject,
            email=email,
            full_name=full_name,
            is_admin=is_admin,
        )
        db.add(user)
    else:
        user.email = email
        user.full_name = full_name
        user.is_admin = is_admin

    user.last_login_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(user)
    return user
