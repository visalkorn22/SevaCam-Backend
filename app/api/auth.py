from fastapi import (
    APIRouter,
    HTTPException,
    Depends,
    Response,
    Header,
    Body,
    Cookie,
)
from pydantic import BaseModel, EmailStr
from typing import Optional
from sqlalchemy.orm import Session
from sqlalchemy import text
from passlib.context import CryptContext
from datetime import datetime, timedelta
import secrets
import uuid
import hashlib
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.database import get_db
from app.core.auth import get_current_user, resolve_token
from app.core.config import settings
from app.core.email import send_email
from app.core.session import create_app_session, set_auth_cookie, utc_now

router = APIRouter(prefix="/api/auth", tags=["auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
RESET_TOKEN_MINUTES = 60
VERIFY_TOKEN_HOURS = 24
MAGIC_LINK_TOKEN_MINUTES = settings.MAGIC_LINK_TOKEN_MINUTES
DEFAULT_APP_TIMEZONE = "Asia/Phnom_Penh"


def is_expired(expires_at: datetime) -> bool:
    now = utc_now()
    if expires_at.tzinfo is None:
        return expires_at < now.replace(tzinfo=None)
    return expires_at < now


def _canonicalize_timezone_name(value: str) -> str:
    segments = []
    for segment in value.split("/"):
        parts = []
        for part in segment.split("_"):
            if part.upper() in {"UTC", "GMT"}:
                parts.append(part.upper())
            else:
                parts.append(part[:1].upper() + part[1:].lower())
        segments.append("_".join(parts))
    return "/".join(segments)


def _normalize_timezone_name(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    candidate = value.strip().replace(" ", "_")
    if not candidate:
        return None

    attempts = [candidate]
    canonical = _canonicalize_timezone_name(candidate)
    if canonical not in attempts:
        attempts.append(canonical)

    for attempt in attempts:
        try:
            ZoneInfo(attempt)
            return attempt
        except ZoneInfoNotFoundError:
            continue

    raise HTTPException(status_code=400, detail="Invalid timezone")


# =========================
# Schemas
# =========================

class SignupBody(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    timezone: Optional[str] = None


class LoginBody(BaseModel):
    email: EmailStr
    password: str


class PasswordResetRequestBody(BaseModel):
    email: EmailStr


class PasswordResetConfirmBody(BaseModel):
    token: str
    new_password: str


class EmailVerificationRequestBody(BaseModel):
    email: EmailStr


class EmailVerificationConfirmBody(BaseModel):
    token: str


class MagicLinkRequestBody(BaseModel):
    email: EmailStr


class MagicLinkConfirmBody(BaseModel):
    token: str


class ProfileUpdateBody(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    timezone: Optional[str] = None


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


# =========================
# Helpers
# =========================


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_verification_token() -> str:
    return secrets.token_urlsafe(32)


def hash_verification_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verification_link(token: str) -> str:
    base = settings.APP_URL.rstrip("/")
    return f"{base}/auth/verify-email?token={token}"


def send_verification_email(recipient: str, token: str) -> None:
    link = _verification_link(token)
    subject = "Verify your email"
    body = (
        "Hello,\n\n"
        "Please verify your email address by clicking the link below:\n"
        f"{link}\n\n"
        f"This link expires in {VERIFY_TOKEN_HOURS} hours.\n\n"
        "If you did not create an account, you can ignore this email."
    )
    send_email(recipient, subject, body)


def create_magic_link_token() -> str:
    return secrets.token_urlsafe(32)


def hash_magic_link_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _magic_link_url(token: str) -> str:
    base = settings.APP_URL.rstrip("/")
    return f"{base}/auth/magic-link?token={token}"


def send_magic_link_email(recipient: str, token: str) -> None:
    link = _magic_link_url(token)
    subject = "Your sign-in link"
    body = (
        "Hello,\n\n"
        "Use the link below to sign in:\n"
        f"{link}\n\n"
        f"This link expires in {MAGIC_LINK_TOKEN_MINUTES} minutes.\n\n"
        "If you did not request this email, you can ignore it."
    )
    send_email(recipient, subject, body)


# =========================
# Signup
# =========================

@router.post("/signup")
def signup(
    response: Response,
    payload: SignupBody = Body(...),
    db: Session = Depends(get_db),
):
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    exists = db.execute(
        text("SELECT 1 FROM users WHERE email = :email"),
        {"email": payload.email},
    ).fetchone()

    if exists:
        raise HTTPException(status_code=409, detail="Email already exists")

    password_hash = pwd_context.hash(payload.password)

    user_id = str(uuid.uuid4())
    role = "customer"

    role_exists = db.execute(
        text("SELECT 1 FROM roles WHERE name = :role"),
        {"role": role},
    ).fetchone()
    if not role_exists:
        raise HTTPException(status_code=500, detail="Default role is not configured")

    user = db.execute(
        text("""
            INSERT INTO users (id, email, full_name, role, phone, timezone, password_hash, email_verified)
            VALUES (:id, :email, :full_name, :role, :phone, :timezone, :password_hash, FALSE)
            RETURNING id, email, full_name, role, phone, avatar_url, timezone
        """),
        {
            "id": user_id,
            "email": payload.email,
            "full_name": payload.full_name,
            "role": role,
            "phone": payload.phone,
            "timezone": _normalize_timezone_name(payload.timezone)
            or DEFAULT_APP_TIMEZONE,
            "password_hash": password_hash,
        },
    ).fetchone()
    verification_token = create_verification_token()
    verification_hash = hash_verification_token(verification_token)
    expires_at = utc_now() + timedelta(hours=VERIFY_TOKEN_HOURS)

    db.execute(
        text(
            """
            INSERT INTO email_verification_tokens (id, user_id, token_hash, expires_at)
            VALUES (:id, :user_id, :token_hash, :expires_at)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": user.id,
            "token_hash": verification_hash,
            "expires_at": expires_at,
        },
    )
    db.commit()

    try:
        send_verification_email(payload.email, verification_token)
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to send verification email")

    response.delete_cookie("auth_token", path="/")
    return {"user": dict(user._mapping), "message": "Verification email sent"}


# =========================
# Login
# =========================

@router.post("/login")
def login(
    response: Response,
    payload: LoginBody = Body(...),
    db: Session = Depends(get_db),
):
    user = db.execute(
        text("""
            SELECT id, email, full_name, role, phone, avatar_url, timezone, password_hash, is_active, email_verified
            FROM users
            WHERE email = :email
        """),
        {"email": payload.email},
    ).fetchone()

    if not user or not user.password_hash or not pwd_context.verify(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email not verified")

    token = create_app_session(user.id, db)
    db.commit()
    set_auth_cookie(response, token)

    return {"user": dict(user._mapping), "token": token}


# =========================
# Passwordless magic link
# =========================

@router.post("/magic-link/request")
def request_magic_link(
    payload: MagicLinkRequestBody,
    db: Session = Depends(get_db),
):
    record = db.execute(
        text("SELECT id, is_active FROM users WHERE email = :email"),
        {"email": payload.email},
    ).fetchone()

    if record and not record.is_active:
        return {"message": "If the account exists, a login link will be sent."}

    user_id = record.id if record else None
    if not user_id:
        role = "customer"
        role_exists = db.execute(
            text("SELECT 1 FROM roles WHERE name = :role"),
            {"role": role},
        ).fetchone()
        if not role_exists:
            raise HTTPException(status_code=500, detail="Default role is not configured")

        user_id = str(uuid.uuid4())
        db.execute(
            text(
                """
                INSERT INTO users (id, email, role, timezone, email_verified, is_active)
                VALUES (:id, :email, :role, :timezone, FALSE, TRUE)
                """
            ),
            {
                "id": user_id,
                "email": payload.email,
                "role": role,
                "timezone": DEFAULT_APP_TIMEZONE,
            },
        )

    magic_token = create_magic_link_token()
    token_hash = hash_magic_link_token(magic_token)
    expires_at = utc_now() + timedelta(minutes=MAGIC_LINK_TOKEN_MINUTES)

    db.execute(
        text(
            """
            INSERT INTO magic_link_tokens (id, user_id, token_hash, expires_at)
            VALUES (:id, :user_id, :token_hash, :expires_at)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": user_id,
            "token_hash": token_hash,
            "expires_at": expires_at,
        },
    )
    db.commit()

    try:
        send_magic_link_email(payload.email, magic_token)
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to send login link")

    return {"message": "If the account exists, a login link will be sent."}


@router.post("/magic-link/confirm")
def confirm_magic_link(
    response: Response,
    payload: MagicLinkConfirmBody,
    db: Session = Depends(get_db),
):
    token_hash = hash_magic_link_token(payload.token)
    record = db.execute(
        text(
            """
            SELECT id, user_id, expires_at, used_at
            FROM magic_link_tokens
            WHERE token_hash = :token_hash
            """
        ),
        {"token_hash": token_hash},
    ).fetchone()

    if not record or record.used_at is not None or is_expired(record.expires_at):
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    user = db.execute(
        text(
            """
            SELECT id, email, full_name, role, phone, avatar_url, timezone, is_active
            FROM users
            WHERE id = :id
            """
        ),
        {"id": record.user_id},
    ).fetchone()

    if not user or not user.is_active:
        raise HTTPException(status_code=403, detail="Account is disabled")

    db.execute(
        text("UPDATE users SET email_verified = TRUE WHERE id = :id"),
        {"id": user.id},
    )
    db.execute(
        text("UPDATE magic_link_tokens SET used_at = :used_at WHERE id = :id"),
        {"used_at": utc_now(), "id": record.id},
    )

    token = create_app_session(user.id, db)
    db.commit()
    set_auth_cookie(response, token)

    return {"user": dict(user._mapping)}


# =========================
# Get current user
# =========================

@router.get("/me")
def me(
    current_user: dict = Depends(get_current_user),
):
    return current_user


@router.patch("/me")
def update_me(
    payload: ProfileUpdateBody,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    updates = []
    params = {"id": current_user["id"]}

    if payload.full_name is not None:
        updates.append("full_name = :full_name")
        params["full_name"] = payload.full_name
    if payload.phone is not None:
        updates.append("phone = :phone")
        params["phone"] = payload.phone
    if payload.avatar_url is not None:
        updates.append("avatar_url = :avatar_url")
        params["avatar_url"] = payload.avatar_url
    if payload.timezone is not None:
        updates.append("timezone = :timezone")
        params["timezone"] = _normalize_timezone_name(payload.timezone)

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    query = f"""
        UPDATE users
        SET {", ".join(updates)}
        WHERE id = :id
        RETURNING id, email, full_name, role, phone, avatar_url, timezone
    """
    updated = db.execute(text(query), params).fetchone()
    db.commit()

    return dict(updated._mapping)


@router.post("/change-password")
def change_password(
    response: Response,
    payload: ChangePasswordBody,
    current_user: dict = Depends(get_current_user),
    authorization: Optional[str] = Header(None),
    auth_token: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    record = db.execute(
        text("SELECT password_hash FROM users WHERE id = :id"),
        {"id": current_user["id"]},
    ).fetchone()

    if not record or not record.password_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    if not pwd_context.verify(payload.current_password, record.password_hash):
        raise HTTPException(status_code=401, detail="Invalid credentials")

    password_hash = pwd_context.hash(payload.new_password)
    db.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :id"),
        {"password_hash": password_hash, "id": current_user["id"]},
    )

    token = resolve_token(authorization, auth_token)
    if token:
        db.execute(
            text(
                """
                DELETE FROM sessions
                WHERE user_id = :user_id AND token != :token
                """
            ),
            {"user_id": current_user["id"], "token": token},
        )
    else:
        db.execute(
            text("DELETE FROM sessions WHERE user_id = :user_id"),
            {"user_id": current_user["id"]},
        )

    db.commit()

    response.delete_cookie("auth_token", path="/")
    set_auth_cookie(response, token or "")

    return {"message": "Password updated"}


@router.post("/password-reset/request")
def request_password_reset(
    payload: PasswordResetRequestBody,
    db: Session = Depends(get_db),
):
    user = db.execute(
        text("SELECT id FROM users WHERE email = :email"),
        {"email": payload.email},
    ).fetchone()

    reset_token = None
    if user:
        reset_token = secrets.token_urlsafe(32)
        token_hash = hash_reset_token(reset_token)
        expires_at = utc_now() + timedelta(minutes=RESET_TOKEN_MINUTES)

        db.execute(
            text(
                """
                INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at)
                VALUES (:id, :user_id, :token_hash, :expires_at)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "user_id": user.id,
                "token_hash": token_hash,
                "expires_at": expires_at,
            },
        )
        db.commit()

    response = {"message": "If the account exists, a reset link will be sent."}
    if settings.DEBUG and reset_token:
        response["reset_token"] = reset_token
        response["expires_in_minutes"] = RESET_TOKEN_MINUTES

    return response


@router.post("/password-reset/confirm")
def confirm_password_reset(
    payload: PasswordResetConfirmBody,
    db: Session = Depends(get_db),
):
    if len(payload.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    token_hash = hash_reset_token(payload.token)
    record = db.execute(
        text(
            """
            SELECT id, user_id, expires_at, used_at
            FROM password_reset_tokens
            WHERE token_hash = :token_hash
            """
        ),
        {"token_hash": token_hash},
    ).fetchone()

    if not record or record.used_at is not None or is_expired(record.expires_at):
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    password_hash = pwd_context.hash(payload.new_password)

    db.execute(
        text("UPDATE users SET password_hash = :password_hash WHERE id = :user_id"),
        {"password_hash": password_hash, "user_id": record.user_id},
    )
    db.execute(
        text("UPDATE password_reset_tokens SET used_at = :used_at WHERE id = :id"),
        {"used_at": utc_now(), "id": record.id},
    )
    db.execute(
        text("DELETE FROM sessions WHERE user_id = :user_id"),
        {"user_id": record.user_id},
    )
    db.commit()

    return {"message": "Password updated"}


# =========================
# Email verification
# =========================

@router.post("/verify-email/request")
def request_email_verification(
    payload: EmailVerificationRequestBody,
    db: Session = Depends(get_db),
):
    record = db.execute(
        text("SELECT id, email_verified FROM users WHERE email = :email"),
        {"email": payload.email},
    ).fetchone()

    if not record or record.email_verified:
        return {"message": "If the account exists, a verification email will be sent."}

    verification_token = create_verification_token()
    verification_hash = hash_verification_token(verification_token)
    expires_at = utc_now() + timedelta(hours=VERIFY_TOKEN_HOURS)

    db.execute(
        text(
            """
            INSERT INTO email_verification_tokens (id, user_id, token_hash, expires_at)
            VALUES (:id, :user_id, :token_hash, :expires_at)
            """
        ),
        {
            "id": str(uuid.uuid4()),
            "user_id": record.id,
            "token_hash": verification_hash,
            "expires_at": expires_at,
        },
    )
    db.commit()

    try:
        send_verification_email(payload.email, verification_token)
    except Exception:
        raise HTTPException(status_code=500, detail="Unable to send verification email")

    return {"message": "If the account exists, a verification email will be sent."}


@router.post("/verify-email/confirm")
def confirm_email_verification(
    payload: EmailVerificationConfirmBody,
    db: Session = Depends(get_db),
):
    token_hash = hash_verification_token(payload.token)
    record = db.execute(
        text(
            """
            SELECT id, user_id, expires_at, used_at
            FROM email_verification_tokens
            WHERE token_hash = :token_hash
            """
        ),
        {"token_hash": token_hash},
    ).fetchone()

    if not record or record.used_at is not None or is_expired(record.expires_at):
        raise HTTPException(status_code=400, detail="Invalid or expired token")

    db.execute(
        text("UPDATE users SET email_verified = TRUE WHERE id = :user_id"),
        {"user_id": record.user_id},
    )
    db.execute(
        text("UPDATE email_verification_tokens SET used_at = :used_at WHERE id = :id"),
        {"used_at": utc_now(), "id": record.id},
    )
    db.commit()

    return {"message": "Email verified"}


# =========================
# Logout
# =========================

@router.post("/logout")
def logout(
    response: Response,
    authorization: Optional[str] = Header(None),
    auth_token: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
):
    token = resolve_token(authorization, auth_token)
    if token:
        db.execute(text("DELETE FROM sessions WHERE token = :token"), {"token": token})
        db.commit()

    response.delete_cookie("auth_token", path="/")
    return {"success": True}


@router.post("/logout-all")
def logout_all(
    response: Response,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    db.execute(
        text("DELETE FROM sessions WHERE user_id = :user_id"),
        {"user_id": current_user["id"]},
    )
    db.commit()
    response.delete_cookie("auth_token", path="/")
    return {"success": True}
