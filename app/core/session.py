import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import Response
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def create_app_session(user_id: str, db: Session) -> str:
    """Insert a new session row and return the raw token. Caller must db.commit()."""
    token = secrets.token_hex(32)
    expires_at = utc_now() + timedelta(days=settings.SESSION_DAYS)
    db.execute(
        text(
            "INSERT INTO sessions (id, user_id, token, expires_at) "
            "VALUES (:id, :user_id, :token, :expires_at)"
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "token": token,
            "expires_at": expires_at,
        },
    )
    return token


def set_auth_cookie(response: Response, token: str) -> None:
    """Set the auth httpOnly cookie using env-driven security settings."""
    response.set_cookie(
        key=settings.COOKIE_NAME,
        value=token,
        httponly=True,
        samesite=settings.COOKIE_SAMESITE,
        secure=settings.COOKIE_SECURE,
        domain=settings.COOKIE_DOMAIN or None,
        path=settings.COOKIE_PATH,
        max_age=settings.SESSION_DAYS * 24 * 60 * 60,
    )
