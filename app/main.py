from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from secrets import compare_digest, token_urlsafe
from typing import Optional
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.auth import (
    authenticate_local_admin,
    get_current_user,
    get_oauth,
    get_optional_user,
    require_admin,
    sso_configured,
    sync_sso_admin_flags,
    upsert_local_admin,
    upsert_user_from_oidc,
)
from app.config import Settings, get_settings
from app.db import SessionLocal, get_db, init_db
from app.env_store import ensure_local_admin_credentials, write_env_file
from app.job_service import create_job, get_job_for_user, list_jobs_for_user, snapshot_job
from app.models import ImportJob, UploadSession, User
from app.settings_store import SETTING_DEFINITIONS, bootstrap_runtime_settings, get_runtime_settings, update_runtime_settings
from app.telegram_export import TelegramArchiveError, parse_telegram_archive
from app.yandex_messenger import YandexMessengerClient, YandexMessengerError


BASE_DIR = Path(__file__).resolve().parents[1]
boot_settings: Settings = get_settings()
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
UPLOAD_CHUNK_SIZE_BYTES = 32 * 1024 * 1024
UPLOAD_SESSION_RETENTION_HOURS = 24
UPLOAD_SESSION_STALE_MINUTES = 10

app = FastAPI(title=boot_settings.app_name)
app.add_middleware(
    SessionMiddleware,
    secret_key=boot_settings.secret_key,
    same_site="lax",
    https_only=boot_settings.public_base_url.startswith("https://"),
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    request.state.csp_nonce = token_urlsafe(16)
    current_settings = get_settings()
    allowed_during_maintenance = {
        "/health",
        "/login",
        "/login/local",
        "/login/dev",
        "/login/sso",
        "/auth/callback",
    }
    if (
        current_settings.maintenance_mode
        and request.url.path not in allowed_during_maintenance
        and not request.url.path.startswith("/static/")
        and request.url.path not in {"/", "/dashboard"}
        and not request.url.path.startswith("/admin")
        and not request.url.path.startswith("/api/")
    ):
        response = templates.TemplateResponse(
            request=request,
            name="maintenance.html",
            context={
                "title": "Migrator · Сервисные работы",
                "csp_nonce": request.state.csp_nonce,
            },
            status_code=503,
        )
    else:
        response = await call_next(request)
    nonce = request.state.csp_nonce
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "connect-src 'self'; "
        "form-action 'self'"
    )
    response.headers.setdefault("Content-Security-Policy", csp)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    return response


@app.on_event("startup")
def on_startup() -> None:
    current_settings = get_settings()
    ensure_local_admin_credentials(current_settings.env_file_path)
    init_db()
    with SessionLocal() as db:
        bootstrap_runtime_settings(db, current_settings)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, user: Optional[User] = Depends(get_optional_user)):
    if user:
        return RedirectResponse(url="/dashboard", status_code=303)
    return RedirectResponse(url="/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    current_settings = get_settings()
    ensure_local_admin_credentials(current_settings.env_file_path)
    current_settings = get_settings()
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "title": "Migrator",
            "sso_ready": sso_configured(current_settings),
            "public_base_url": current_settings.public_base_url,
            "local_admin_login": current_settings.local_admin_login,
            "csrf_token": _ensure_csrf_token(request),
            "csp_nonce": request.state.csp_nonce,
        },
    )


@app.post("/login/local")
@app.post("/login/dev")
async def local_login(
    request: Request,
    csrf_token: str = Form(...),
    login: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    current_settings = get_settings()
    ensure_local_admin_credentials(current_settings.env_file_path)
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=csrf_token, settings=current_settings)

    normalized_login = login.strip()
    if not normalized_login or not password:
        raise HTTPException(status_code=400, detail="Укажите логин и пароль администратора.")

    if not authenticate_local_admin(login=normalized_login, password=password, settings=current_settings):
        raise HTTPException(status_code=401, detail="Неверный логин или пароль администратора.")

    user = upsert_local_admin(db, current_settings)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@app.get("/login/sso")
async def login_sso(request: Request):
    current_settings = get_settings()
    if not sso_configured(current_settings):
        raise HTTPException(status_code=503, detail="SSO пока не настроен.")
    oauth = get_oauth(current_settings)
    redirect_uri = f"{current_settings.public_base_url}/auth/callback"
    return await oauth.oidc.authorize_redirect(request, redirect_uri)


@app.get("/auth/callback")
async def auth_callback(request: Request, db: Session = Depends(get_db)):
    current_settings = get_settings()
    if not sso_configured(current_settings):
        raise HTTPException(status_code=503, detail="SSO пока не настроен.")
    oauth = get_oauth(current_settings)
    token = await oauth.oidc.authorize_access_token(request)
    userinfo = token.get("userinfo")
    if not userinfo:
        userinfo = await oauth.oidc.parse_id_token(request, token)
    user = upsert_user_from_oidc(db, dict(userinfo), current_settings)
    request.session["user_id"] = user.id
    return RedirectResponse(url="/dashboard", status_code=303)


@app.post("/logout")
async def logout(request: Request, csrf_token: str = Form(...)):
    _validate_csrf(request, submitted_token=csrf_token, settings=get_settings())
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _mark_stale_upload_sessions(db)
    jobs = [snapshot_job(db, job).__dict__ for job in list_jobs_for_user(db, user)]
    active_upload_session = _serialize_upload_session(_get_active_upload_session(db, user=user))
    runtime = get_runtime_settings(db, current_settings)
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "title": "Migrator",
            "user": user,
            "jobs": jobs,
            "is_admin": user.is_admin,
            "max_parallel_jobs": runtime["max_parallel_jobs"],
            "max_parallel_jobs_per_user": runtime["max_parallel_jobs_per_user"],
            "can_run_import": not user.external_subject.startswith("local-"),
            "active_upload_session": active_upload_session,
            "csrf_token": _ensure_csrf_token(request),
            "csp_nonce": request.state.csp_nonce,
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_panel(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    current_settings = get_settings()
    ensure_local_admin_credentials(current_settings.env_file_path)
    current_settings = get_settings()
    runtime = get_runtime_settings(db, current_settings)
    jobs = db.execute(select(ImportJob).order_by(desc(ImportJob.created_at)).limit(100)).scalars().all()
    users = db.execute(select(User).order_by(desc(User.last_login_at))).scalars().all()
    total_uploads_count = db.execute(select(func.count()).select_from(ImportJob)).scalar_one()
    successful_chats_count = db.execute(
        select(func.count()).select_from(ImportJob).where(ImportJob.state == "completed")
    ).scalar_one()
    settings_rows = {
        definition.key: {
            "value": runtime.get(definition.key, definition.default),
            "description": definition.description,
            "is_secret": definition.is_secret,
        }
        for definition in SETTING_DEFINITIONS
    }
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={
            "title": "Админка · Migrator",
            "user": user,
            "jobs": [snapshot_job(db, job).__dict__ for job in jobs],
            "users": users,
            "settings_rows": settings_rows,
            "access_settings": _access_settings_context(current_settings),
            "total_uploads_count": total_uploads_count,
            "successful_chats_count": successful_chats_count,
            "csrf_token": _ensure_csrf_token(request),
            "csp_nonce": request.state.csp_nonce,
        },
    )


@app.get("/admin/export/completed-chats.csv")
async def export_completed_chats_csv(db: Session = Depends(get_db), user: User = Depends(require_admin)):
    jobs = (
        db.execute(
            select(ImportJob, User)
            .join(User, User.id == ImportJob.owner_id)
            .where(ImportJob.state == "completed")
            .order_by(desc(ImportJob.finished_at), desc(ImportJob.created_at))
        )
        .all()
    )

    seen_names: set[str] = set()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Наименование чата", "chat_id", "Ссылка", "Создатель", "Дата"])

    for job, owner in jobs:
        chat_name = (job.target_value or "").strip()
        if not chat_name or chat_name in seen_names:
            continue
        seen_names.add(chat_name)
        chat_id = (job.resolved_chat_id or "").strip()
        chat_link = _build_chat_link(chat_id)
        creator = (owner.full_name or "").strip() or owner.email
        job_date = job.finished_at or job.created_at
        writer.writerow(
            [
                chat_name,
                chat_id,
                chat_link,
                creator,
                job_date.astimezone().strftime("%Y-%m-%d %H:%M:%S") if job_date else "",
            ]
        )

    filename = f"completed-chats-{datetime.now().strftime('%Y-%m-%d')}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/settings")
async def save_admin_settings(
    request: Request,
    csrf_token: str = Form(...),
    yandex_bot_token: str = Form(""),
    yandex_api_base: str = Form(...),
    max_parallel_jobs: str = Form(...),
    max_parallel_jobs_per_user: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    _validate_csrf(request, submitted_token=csrf_token, settings=get_settings())
    runtime = get_runtime_settings(db, get_settings())
    try:
        global_limit = max(1, int(max_parallel_jobs))
        per_user_limit = max(1, int(max_parallel_jobs_per_user))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Лимиты параллелизма должны быть целыми числами.") from exc
    if per_user_limit > global_limit:
        raise HTTPException(
            status_code=400,
            detail="Лимит на пользователя не может быть больше общего лимита параллельных задач.",
        )
    payload = {
        "yandex_bot_token": yandex_bot_token or runtime.get("yandex_bot_token", ""),
        "yandex_api_base": yandex_api_base,
        "max_parallel_jobs": str(global_limit),
        "max_parallel_jobs_per_user": str(per_user_limit),
    }
    update_runtime_settings(db, payload, updated_by_user_id=user.id)
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/access-settings")
async def save_access_settings(
    request: Request,
    csrf_token: str = Form(...),
    public_base_url: str = Form(...),
    sso_enabled: str = Form("false"),
    oidc_server_metadata_url: str = Form(""),
    oidc_client_id: str = Form(""),
    oidc_client_secret: str = Form(""),
    oidc_scope: str = Form("openid profile email"),
    admin_emails: str = Form(""),
    admin_domains: str = Form(""),
    local_admin_login: str = Form(...),
    local_admin_password: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=csrf_token, settings=current_settings)

    effective_base_url = public_base_url.strip().rstrip("/")
    if not effective_base_url:
        raise HTTPException(status_code=400, detail="Укажите внешний URL сервиса.")

    effective_login = local_admin_login.strip()
    effective_password = local_admin_password.strip() or current_settings.local_admin_password
    effective_client_secret = oidc_client_secret.strip() or current_settings.oidc_client_secret
    if not effective_login or not effective_password:
        raise HTTPException(status_code=400, detail="Локальный admin login и password не могут быть пустыми.")

    write_env_file(
        current_settings.env_file_path,
        {
            "PUBLIC_BASE_URL": effective_base_url,
            "SSO_ENABLED": "true" if sso_enabled == "true" else "false",
            "OIDC_SERVER_METADATA_URL": oidc_server_metadata_url.strip(),
            "OIDC_CLIENT_ID": oidc_client_id.strip(),
            "OIDC_CLIENT_SECRET": effective_client_secret,
            "OIDC_SCOPE": oidc_scope.strip() or "openid profile email",
            "ADMIN_EMAILS": admin_emails.strip(),
            "ADMIN_DOMAINS": admin_domains.strip(),
            "LOCAL_ADMIN_LOGIN": effective_login,
            "LOCAL_ADMIN_PASSWORD": effective_password,
        },
    )
    sync_sso_admin_flags(db, get_settings())
    return RedirectResponse(url="/admin", status_code=303)

@app.post("/api/uploads")
async def create_upload_session(
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=request.headers.get("X-CSRF-Token", ""), settings=current_settings)
    payload = await _read_json_body(request)
    archive_name = str(payload.get("archive_name") or "").strip()
    total_bytes = _safe_positive_int(payload.get("total_bytes"))

    if not archive_name.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Нужен ZIP-архив из Telegram Desktop export.")
    if total_bytes <= 0:
        raise HTTPException(status_code=400, detail="Не удалось определить размер архива.")

    max_archive_size_bytes = max(current_settings.max_archive_size_mb, 1) * 1024 * 1024
    if total_bytes > max_archive_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Архив слишком большой. Максимальный размер: {current_settings.max_archive_size_mb} MB.",
        )

    _cleanup_stale_upload_sessions(db)

    user_upload_dir = current_settings.upload_dir / user.email
    temp_dir = user_upload_dir / ".chunked"
    temp_dir.mkdir(parents=True, exist_ok=True)
    session = UploadSession(
        owner_id=user.id,
        archive_name=archive_name,
        temp_path=str((temp_dir / f"{token_urlsafe(12)}.part").resolve()),
        total_bytes=total_bytes,
        received_bytes=0,
        state="uploading",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return {
        "upload_id": session.id,
        "chunk_size": UPLOAD_CHUNK_SIZE_BYTES,
        "total_bytes": session.total_bytes,
        "archive_name": session.archive_name,
    }


@app.post("/api/uploads/{upload_id}/chunks")
async def upload_chunk(
    upload_id: str,
    request: Request,
    chunk_index: int,
    offset: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=request.headers.get("X-CSRF-Token", ""), settings=current_settings)
    session = _get_upload_session(db, upload_id=upload_id, user=user)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена.")
    if session.state not in {"uploading", "uploaded"}:
        raise HTTPException(status_code=400, detail="Эта сессия загрузки больше не активна.")
    if offset != session.received_bytes:
        raise HTTPException(
            status_code=409,
            detail=f"Неверное смещение чанка. Ожидается offset={session.received_bytes}.",
        )

    chunk = await request.body()
    if not chunk:
        raise HTTPException(status_code=400, detail="Получен пустой чанк.")
    if session.received_bytes + len(chunk) > session.total_bytes:
        raise HTTPException(status_code=400, detail="Размер загружаемого чанка выходит за пределы архива.")

    temp_path = Path(session.temp_path)
    temp_path.parent.mkdir(parents=True, exist_ok=True)
    with temp_path.open("ab") as output:
        output.write(chunk)

    session.received_bytes += len(chunk)
    if session.received_bytes == session.total_bytes:
        session.state = "uploaded"
    db.commit()
    return {
        "upload_id": session.id,
        "chunk_index": chunk_index,
        "received_bytes": session.received_bytes,
        "total_bytes": session.total_bytes,
        "complete": session.received_bytes == session.total_bytes,
    }


@app.post("/api/uploads/{upload_id}/complete")
async def complete_upload_session(
    upload_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=request.headers.get("X-CSRF-Token", ""), settings=current_settings)
    session = _get_upload_session(db, upload_id=upload_id, user=user)
    if session is None:
        raise HTTPException(status_code=404, detail="Сессия загрузки не найдена.")
    if session.received_bytes != session.total_bytes:
        raise HTTPException(status_code=400, detail="Архив ещё не загружен полностью.")

    payload = await _read_json_body(request)
    target = str(payload.get("target") or "")
    target_kind = str(payload.get("target_kind") or "chat")
    additional_members = str(payload.get("additional_members") or "")

    effective_target, additional_member_logins = _prepare_import_request(
        db=db,
        settings=current_settings,
        user=user,
        target=target,
        target_kind=target_kind,
        additional_members=additional_members,
    )

    temp_path = Path(session.temp_path)
    if not temp_path.exists():
        session.state = "failed"
        session.error = "Временный файл загрузки не найден."
        db.commit()
        raise HTTPException(status_code=400, detail="Временный файл загрузки не найден.")

    stored_path = _build_unique_archive_path(current_settings=current_settings, user=user, filename=session.archive_name)
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.replace(stored_path)

    try:
        job = _create_job_from_archive_path(
            db=db,
            owner=user,
            archive_name=session.archive_name,
            archive_path=stored_path,
            target_kind=target_kind,
            target_value=effective_target,
            additional_member_logins=additional_member_logins,
        )
    except HTTPException as exc:
        session.state = "failed"
        session.error = str(exc.detail)
        db.commit()
        raise

    db.delete(session)
    db.commit()
    return {"job_id": job.id, "message": "Импорт поставлен в очередь"}


@app.delete("/api/uploads/{upload_id}")
async def delete_upload_session(
    upload_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=request.headers.get("X-CSRF-Token", ""), settings=current_settings)
    session = _get_upload_session(db, upload_id=upload_id, user=user)
    if session is None:
        return {"deleted": True}
    Path(session.temp_path).unlink(missing_ok=True)
    db.delete(session)
    db.commit()
    return {"deleted": True}


@app.post("/api/imports")
async def create_import_api(
    request: Request,
    archive: UploadFile = File(...),
    csrf_token: str = Form(...),
    target: str = Form(...),
    target_kind: str = Form("chat"),
    additional_members: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    current_settings = get_settings()
    _validate_csrf(request, submitted_token=csrf_token, settings=current_settings)
    filename = archive.filename or ""
    effective_target, additional_member_logins = _prepare_import_request(
        db=db,
        settings=current_settings,
        user=user,
        target=target,
        target_kind=target_kind,
        additional_members=additional_members,
    )
    stored_path = _build_unique_archive_path(current_settings=current_settings, user=user, filename=filename)

    try:
        await _store_archive_upload(
            archive=archive,
            destination=stored_path,
            max_archive_size_mb=current_settings.max_archive_size_mb,
        )
        job = _create_job_from_archive_path(
            db=db,
            owner=user,
            archive_name=filename,
            archive_path=stored_path,
            target_kind=target_kind,
            target_value=effective_target,
            additional_member_logins=additional_member_logins,
        )
    except HTTPException:
        raise
    finally:
        await archive.close()
    return {"job_id": job.id, "message": "Импорт поставлен в очередь"}


@app.get("/api/imports/{job_id}")
async def get_import_status(
    request: Request,
    job_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    job = get_job_for_user(db, job_id=job_id, user=user)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    return snapshot_job(db, job).__dict__


@app.post("/api/imports/{job_id}/pause")
async def pause_import(
    job_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    _validate_csrf(request, submitted_token=csrf_token, settings=get_settings())
    job = get_job_for_user(db, job_id=job_id, user=user)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")

    if job.state == "queued":
        job.state = "paused"
        job.detail = "Импорт остановлен пользователем до запуска."
        job.finished_at = datetime.now(timezone.utc)
    elif job.state == "running":
        job.state = "pause_requested"
        job.detail = "Останавливаю импорт после текущего сообщения."
    elif job.state == "pause_requested":
        pass
    else:
        raise HTTPException(status_code=400, detail="Эту задачу нельзя остановить.")

    db.commit()
    return snapshot_job(db, job).__dict__


@app.post("/api/imports/{job_id}/resume")
async def resume_import(
    job_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    _validate_csrf(request, submitted_token=csrf_token, settings=get_settings())
    job = get_job_for_user(db, job_id=job_id, user=user)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    if job.state not in {"paused", "failed"}:
        raise HTTPException(status_code=400, detail="Продолжить можно только остановленную или упавшую задачу.")
    if not job.archive_path or not Path(job.archive_path).exists():
        raise HTTPException(status_code=400, detail="Архив для продолжения уже удалён. Загрузите его заново.")

    job.state = "queued"
    job.detail = (
        f"Поставлено в очередь на продолжение с {job.processed_messages} из {job.total_messages}"
        if job.total_messages
        else "Поставлено в очередь на продолжение"
    )
    job.finished_at = None
    db.commit()
    return snapshot_job(db, job).__dict__


@app.post("/api/imports/{job_id}/retry")
async def retry_import_links(
    job_id: str,
    request: Request,
    csrf_token: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    maintenance_response = _maintenance_access_response(request, user)
    if maintenance_response is not None:
        return maintenance_response
    _validate_csrf(request, submitted_token=csrf_token, settings=get_settings())
    job = get_job_for_user(db, job_id=job_id, user=user)
    if not job:
        raise HTTPException(status_code=404, detail="Задача не найдена.")
    snapshot = snapshot_job(db, job)
    if not snapshot.can_retry:
        raise HTTPException(status_code=400, detail="Для этой задачи нечего повторять.")
    if not job.archive_path or not Path(job.archive_path).exists():
        raise HTTPException(status_code=400, detail="Архив для повторной попытки уже удалён. Загрузите его заново.")

    job.state = "queued"
    job.finished_at = None
    job.detail = "Поставлено в очередь на повторную отправку ссылок по вложениям."
    db.commit()
    return snapshot_job(db, job).__dict__


@app.get("/health")
async def health():
    current_settings = get_settings()
    return {"status": "ok", "maintenance_mode": current_settings.maintenance_mode}


def _mask_secret(value: str) -> str:
    if not value:
        return "не задан"
    if len(value) <= 6:
        return "*" * len(value)
    return f"{value[:3]}***{value[-3:]}"


def _build_chat_link(chat_id: str) -> str:
    cleaned = (chat_id or "").strip()
    if not cleaned:
        return ""
    return f"https://messenger.yandex.ru/chat/#/chats/{cleaned}"


def _maintenance_access_response(request: Request, user: User):
    settings = get_settings()
    if not settings.maintenance_mode or user.is_admin:
        return None
    if request.url.path.startswith("/api/"):
        raise HTTPException(status_code=503, detail="Сервис временно закрыт на работы.")
    return templates.TemplateResponse(
        request=request,
        name="maintenance.html",
        context={
            "title": "Migrator · Сервисные работы",
            "csp_nonce": request.state.csp_nonce,
        },
        status_code=503,
    )


def _access_settings_context(settings: Settings) -> dict[str, str | bool]:
    return {
        "enabled": sso_configured(settings),
        "sso_enabled": settings.sso_enabled,
        "metadata_url": settings.oidc_server_metadata_url,
        "client_id": settings.oidc_client_id,
        "client_secret_masked": _mask_secret(settings.oidc_client_secret),
        "scope": settings.oidc_scope,
        "callback_url": f"{settings.public_base_url}/auth/callback",
        "login_url": f"{settings.public_base_url}/login/sso",
        "logout_url": f"{settings.public_base_url}/logout",
        "admin_emails": ", ".join(settings.admin_emails),
        "admin_domains": ", ".join(settings.admin_domains),
        "public_base_url": settings.public_base_url,
        "local_admin_login": settings.local_admin_login,
        "local_admin_password_masked": _mask_secret(settings.local_admin_password),
    }


def _ensure_csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def _validate_csrf(request: Request, *, submitted_token: str, settings: Settings) -> None:
    expected_token = str(request.session.get("csrf_token") or "")
    if not expected_token or not submitted_token or not compare_digest(expected_token, submitted_token):
        raise HTTPException(status_code=403, detail="CSRF validation failed.")
    _validate_request_origin(request, settings)


def _validate_request_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    referer = request.headers.get("referer")
    if origin and not _same_origin(origin, settings.public_base_url):
        raise HTTPException(status_code=403, detail="Origin is not allowed.")
    if not origin and referer and not _same_origin(referer, settings.public_base_url):
        raise HTTPException(status_code=403, detail="Referer is not allowed.")


def _same_origin(candidate: str, expected_base_url: str) -> bool:
    candidate_parts = urlparse(candidate)
    expected_parts = urlparse(expected_base_url)
    return (
        candidate_parts.scheme.lower() == expected_parts.scheme.lower()
        and candidate_parts.netloc.lower() == expected_parts.netloc.lower()
    )


def _parse_member_emails(raw_value: str) -> list[str]:
    separators_normalized = raw_value.replace("\n", ",").replace(";", ",")
    result: list[str] = []
    seen: set[str] = set()
    for item in separators_normalized.split(","):
        email = item.strip().lower()
        if not email or email in seen:
            continue
        seen.add(email)
        result.append(email)
    return result


def _safe_positive_int(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if parsed > 0 else 0


async def _read_json_body(request: Request) -> dict[str, object]:
    try:
        payload = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Некорректный JSON в теле запроса.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Ожидается JSON-объект.")
    return payload


def _build_unique_archive_path(*, current_settings: Settings, user: User, filename: str) -> Path:
    archive_dir = current_settings.upload_dir / user.email
    archive_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(filename or "telegram-import.zip").name
    unique_path = archive_dir / f"{user.id}-{Path(safe_name).stem}"
    stored_path = unique_path.with_suffix(".zip")
    counter = 1
    while stored_path.exists():
        stored_path = unique_path.with_name(f"{unique_path.stem}-{counter}").with_suffix(".zip")
        counter += 1
    return stored_path


async def _store_archive_upload(*, archive: UploadFile, destination: Path, max_archive_size_mb: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        max_archive_size_bytes = max(max_archive_size_mb, 1) * 1024 * 1024
        written_bytes = 0
        with destination.open("wb") as output:
            while True:
                chunk = archive.file.read(1024 * 1024)
                if not chunk:
                    break
                written_bytes += len(chunk)
                if written_bytes > max_archive_size_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"Архив слишком большой. Максимальный размер: {max_archive_size_mb} MB.",
                    )
                output.write(chunk)
    except HTTPException:
        destination.unlink(missing_ok=True)
        raise


def _prepare_import_request(
    *,
    db: Session,
    settings: Settings,
    user: User,
    target: str,
    target_kind: str,
    additional_members: str,
) -> tuple[str, list[str]]:
    if target_kind not in {"chat", "channel"}:
        raise HTTPException(status_code=400, detail="Неверный тип цели.")

    runtime = get_runtime_settings(db, settings)
    if not runtime.get("yandex_bot_token"):
        raise HTTPException(status_code=400, detail="В админке не задан OAuth-токен бота Яндекс Мессенджера.")

    if user.external_subject.startswith("local-"):
        raise HTTPException(
            status_code=400,
            detail="Импорт запускается только под SSO-пользователем: его нужно назначить администратором нового чата.",
        )

    yandex_client = YandexMessengerClient(api_base=runtime.get("yandex_api_base") or settings.yandex_api_base)
    try:
        effective_target = yandex_client.normalize_target_name(target)
        yandex_client.ensure_org_login(user.email)
        additional_member_emails = _parse_member_emails(additional_members)
        additional_member_logins = [yandex_client.ensure_org_login(email) for email in additional_member_emails]
    except YandexMessengerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return effective_target, additional_member_logins


def _create_job_from_archive_path(
    *,
    db: Session,
    owner: User,
    archive_name: str,
    archive_path: Path,
    target_kind: str,
    target_value: str,
    additional_member_logins: list[str],
) -> ImportJob:
    try:
        export = parse_telegram_archive(archive_path)
        if not export.messages:
            raise TelegramArchiveError("В архиве нет сообщений для импорта.")
    except TelegramArchiveError as exc:
        archive_path.unlink(missing_ok=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        return create_job(
            db,
            owner=owner,
            archive_name=archive_name,
            archive_path=archive_path,
            target_kind=target_kind,
            target_value=target_value,
            additional_member_emails=additional_member_logins,
        )
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _get_upload_session(db: Session, *, upload_id: str, user: User) -> UploadSession | None:
    return db.execute(
        select(UploadSession).where(UploadSession.id == upload_id, UploadSession.owner_id == user.id)
    ).scalar_one_or_none()


def _get_active_upload_session(db: Session, *, user: User) -> UploadSession | None:
    return db.execute(
        select(UploadSession)
        .where(UploadSession.owner_id == user.id, UploadSession.state.in_(("uploading", "uploaded")))
        .order_by(desc(UploadSession.updated_at), desc(UploadSession.created_at))
        .limit(1)
    ).scalar_one_or_none()


def _serialize_upload_session(session: UploadSession | None) -> dict[str, object] | None:
    if session is None:
        return None
    return {
        "id": session.id,
        "archive_name": session.archive_name,
        "state": session.state,
        "total_bytes": int(session.total_bytes or 0),
        "received_bytes": int(session.received_bytes or 0),
        "error": session.error,
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }


def _mark_stale_upload_sessions(db: Session) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=UPLOAD_SESSION_STALE_MINUTES)
    sessions = (
        db.execute(
            select(UploadSession).where(
                UploadSession.state.in_(("uploading", "uploaded")),
                UploadSession.updated_at < cutoff,
            )
        )
        .scalars()
        .all()
    )
    if not sessions:
        return
    for session in sessions:
        session.state = "failed"
        session.error = "Загрузка архива прервана. Не закрывайте вкладку до начала задачи."
    db.commit()


def _cleanup_stale_upload_sessions(db: Session) -> None:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=UPLOAD_SESSION_RETENTION_HOURS)
    sessions = (
        db.execute(select(UploadSession).where(UploadSession.updated_at < cutoff))
        .scalars()
        .all()
    )
    if not sessions:
        return
    for session in sessions:
        Path(session.temp_path).unlink(missing_ok=True)
        db.delete(session)
    db.commit()
