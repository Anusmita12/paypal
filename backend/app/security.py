"""Demo-session cookies, CSRF (double submit with an HMAC-derived token), origin checks, and rate limits."""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import Depends, Request, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.models import DemoSession
from app.seed import seed_session
from app.suppliers import utcnow

SESSION_COOKIE = "tp_session"
CSRF_COOKIE = "tp_csrf"
CSRF_HEADER = "x-csrf-token"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def csrf_for(token: str, settings: Settings) -> str:
    return hmac.new(settings.session_secret.encode(), f"csrf:{token}".encode(), hashlib.sha256).hexdigest()


def client_ip_hash(request: Request, settings: Settings) -> str:
    ip = request.client.host if request.client else "unknown"
    return hmac.new(settings.session_secret.encode(), f"ip:{ip}".encode(), hashlib.sha256).hexdigest()


def check_origin(request: Request, settings: Settings) -> None:
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") not in settings.origins:
        raise AppError("bad_origin", "Cross-site request rejected.", 403)


def create_session(db: Session, request: Request, response: Response, settings: Settings) -> tuple[DemoSession, str]:
    check_origin(request, settings)
    ip_hash = client_ip_hash(request, settings)
    now = utcnow()
    recent = db.scalar(
        select(func.count()).select_from(DemoSession).where(
            DemoSession.ip_hash == ip_hash, DemoSession.created_at >= now - timedelta(hours=1)
        )
    )
    if recent >= settings.max_sessions_per_ip_per_hour:
        raise AppError("rate_limited", "Too many demo sessions from this address; try again later.", 429)
    token = secrets.token_urlsafe(32)
    tz = ZoneInfo(settings.business_timezone)
    business_date = settings.demo_business_date or datetime.now(tz).date()
    s = DemoSession(
        token_hash=_sha(token),
        ip_hash=ip_hash,
        business_date=business_date,
        timezone=settings.business_timezone,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
    )
    db.add(s)
    db.flush()
    seed_session(db, s.id, business_date)
    db.commit()
    db.refresh(s)
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, samesite="lax",
                        secure=settings.secure_cookies, path="/")
    csrf = csrf_for(token, settings)
    response.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, samesite="strict",
                        secure=settings.secure_cookies, path="/")
    return s, csrf


def current_session(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> DemoSession:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise AppError("no_session", "Start a demo session first.", 401)
    s = db.scalar(select(DemoSession).where(DemoSession.token_hash == _sha(token)))
    if not s or s.expires_at < utcnow():
        raise AppError("no_session", "Demo session expired; start a new one.", 401)
    if request.method not in SAFE_METHODS:
        check_origin(request, settings)
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not hmac.compare_digest(sent, csrf_for(token, settings)):
            raise AppError("csrf_failed", "Missing or invalid CSRF token.", 403)
    return s
