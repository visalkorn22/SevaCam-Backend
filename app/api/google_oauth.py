import base64
import hashlib
import hmac as hmac_module
import json
import secrets
import time
import uuid
from typing import Optional
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from jose import JWTError
from jose import jwk as jose_jwk
from jose import jwt as jose_jwt
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.session import create_app_session, set_auth_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_JWKS_URL = "https://www.googleapis.com/oauth2/v3/certs"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"
GOOGLE_ISSUER = "https://accounts.google.com"
DEFAULT_APP_TIMEZONE = "Asia/Phnom_Penh"
STATE_TTL_SECONDS = 300


# =========================
# Internal helpers
# =========================

def _frontend_error_url(mode: str, error: str) -> str:
    base = settings.FRONTEND_URL.rstrip("/")
    return f"{base}/auth?mode={mode}&error={error}"


def _make_state(mode: str) -> str:
    """Generate a signed OAuth state token with embedded nonce (TTL: 5 minutes)."""
    nonce = secrets.token_urlsafe(32)
    payload = json.dumps({
        "nonce": nonce,
        "mode": mode,
        "exp": int(time.time()) + STATE_TTL_SECONDS,
    })
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac_module.new(settings.SECRET_KEY.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


def _verify_state(state: str) -> dict:
    """Validate signed state token. Returns payload dict or raises ValueError."""
    try:
        b64, sig = state.rsplit(".", 1)
    except ValueError:
        raise ValueError("Malformed state: missing separator")

    expected = hmac_module.new(
        settings.SECRET_KEY.encode(), b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac_module.compare_digest(sig, expected):
        raise ValueError("Invalid state signature")

    padded = b64 + "=" * (4 - len(b64) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    except Exception:
        raise ValueError("Malformed state payload")

    if payload.get("exp", 0) < time.time():
        raise ValueError("State expired")

    return payload


def _decode_state_payload_unsafe(state: str) -> dict:
    """Decode state payload WITHOUT signature check — only call on a state we just created."""
    b64 = state.rsplit(".", 1)[0]
    padded = b64 + "=" * (4 - len(b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode())


def _exchange_code(code: str) -> dict:
    """Exchange authorization code for tokens via Google's token endpoint."""
    res = httpx.post(
        GOOGLE_TOKEN_URL,
        data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": settings.GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
        timeout=10.0,
    )
    if res.status_code != 200:
        import logging
        logging.error(f"[Google OAuth] Token exchange failed ({res.status_code}): {res.text}")
        raise ValueError(f"Token exchange failed ({res.status_code}): {res.text}")
    return res.json()


def _verify_google_id_token(id_token_str: str, nonce: str, access_token: str = "") -> dict:
    """Verify Google ID token signature with JWKS and validate all claims."""
    try:
        header = jose_jwt.get_unverified_header(id_token_str)
    except JWTError as exc:
        raise ValueError(f"Cannot parse token header: {exc}")

    kid = header.get("kid")

    # Fetch Google's public JWKS for signature verification
    try:
        jwks_res = httpx.get(GOOGLE_JWKS_URL, timeout=10.0)
        jwks_res.raise_for_status()
        jwks = jwks_res.json()
    except Exception as exc:
        raise ValueError(f"Failed to fetch Google public keys: {exc}")

    key_data = next(
        (k for k in jwks.get("keys", []) if k.get("kid") == kid), None
    )
    if not key_data:
        raise ValueError("No matching public key found for token kid")

    # Verify RS256 signature and standard OIDC claims (iss, aud, exp)
    try:
        rsa_key = jose_jwk.construct(key_data, algorithm="RS256")
        claims = jose_jwt.decode(
            id_token_str,
            rsa_key,
            algorithms=["RS256"],
            audience=settings.GOOGLE_CLIENT_ID,
            issuer=GOOGLE_ISSUER,
            access_token=access_token or None,
        )
    except JWTError as exc:
        raise ValueError(f"Token verification failed: {exc}")

    # Validate nonce for replay protection
    if claims.get("nonce") != nonce:
        raise ValueError("Nonce mismatch — possible replay attack")

    return claims


def _get_userinfo(access_token: str) -> dict:
    """Fetch profile enrichment from userinfo endpoint. Non-fatal — returns {} on failure."""
    try:
        res = httpx.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        if res.status_code == 200:
            return res.json()
    except Exception:
        pass
    return {}


# =========================
# Endpoints
# =========================

@router.get("/google/start")
def google_start(mode: str = "login"):
    """Redirect the browser to Google's OAuth 2.0 authorization endpoint."""
    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return RedirectResponse(
            _frontend_error_url("login", "oauth_not_configured"), status_code=302
        )

    if mode not in ("login", "signup"):
        mode = "login"

    state = _make_state(mode)
    nonce = _decode_state_payload_unsafe(state)["nonce"]

    params = urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": settings.GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    })
    return RedirectResponse(f"{GOOGLE_AUTH_URL}?{params}", status_code=302)


@router.get("/google/callback")
def google_callback(
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """
    Google OAuth 2.0 callback. Validates state, exchanges code, verifies id_token,
    finds or creates user, creates a session, sets cookie, redirects to frontend.
    """
    mode = "login"

    if not settings.GOOGLE_CLIENT_ID or not settings.GOOGLE_CLIENT_SECRET:
        return RedirectResponse(
            _frontend_error_url(mode, "oauth_not_configured"), status_code=302
        )

    # Reject if Google returned an error or required params are missing
    if error or not code or not state:
        return RedirectResponse(
            _frontend_error_url(mode, "state_invalid"), status_code=302
        )

    # 1. Validate signed state (CSRF protection)
    try:
        state_payload = _verify_state(state)
    except ValueError:
        return RedirectResponse(
            _frontend_error_url(mode, "state_invalid"), status_code=302
        )

    mode = state_payload.get("mode", "login")
    nonce = state_payload["nonce"]

    # 2. Exchange authorization code for tokens (server-to-server)
    try:
        tokens = _exchange_code(code)
    except ValueError:
        return RedirectResponse(
            _frontend_error_url(mode, "google_failed"), status_code=302
        )

    id_token_str = tokens.get("id_token")
    access_token = tokens.get("access_token")
    if not id_token_str or not access_token:
        return RedirectResponse(
            _frontend_error_url(mode, "google_failed"), status_code=302
        )

    # 3. Verify id_token signature and claims (iss, aud, exp, nonce)
    try:
        claims = _verify_google_id_token(id_token_str, nonce, access_token)
    except ValueError:
        return RedirectResponse(
            _frontend_error_url(mode, "google_failed"), status_code=302
        )

    email = claims.get("email")
    email_verified = claims.get("email_verified", False)
    if not email or not email_verified:
        return RedirectResponse(
            _frontend_error_url(mode, "google_failed"), status_code=302
        )

    # 4. Fetch profile enrichment (non-fatal — identity already established from id_token)
    userinfo = _get_userinfo(access_token)
    full_name: Optional[str] = userinfo.get("name")
    avatar_url: Optional[str] = userinfo.get("picture")

    # 5. Find or create local user by email (idempotent)
    user = db.execute(
        text("""
            SELECT id, email, full_name, role, phone, avatar_url, timezone,
                   is_active, email_verified
            FROM users WHERE email = :email
        """),
        {"email": email},
    ).fetchone()

    if not user:
        role = "customer"
        role_exists = db.execute(
            text("SELECT 1 FROM roles WHERE name = :role"), {"role": role}
        ).fetchone()
        if not role_exists:
            return RedirectResponse(
                _frontend_error_url(mode, "google_failed"), status_code=302
            )

        user_id = str(uuid.uuid4())
        user = db.execute(
            text("""
                INSERT INTO users
                    (id, email, full_name, role, avatar_url, timezone, email_verified, is_active)
                VALUES
                    (:id, :email, :full_name, :role, :avatar_url, :timezone, TRUE, TRUE)
                RETURNING id, email, full_name, role, phone, avatar_url, timezone,
                          is_active, email_verified
            """),
            {
                "id": user_id,
                "email": email,
                "full_name": full_name,
                "role": role,
                "avatar_url": avatar_url,
                "timezone": DEFAULT_APP_TIMEZONE,
            },
        ).fetchone()
    else:
        if not user.is_active:
            return RedirectResponse(
                _frontend_error_url(mode, "account_inactive"), status_code=302
            )

        updates = []
        params: dict = {"id": user.id}

        if not user.email_verified:
            updates.append("email_verified = TRUE")
        if avatar_url and not user.avatar_url:
            updates.append("avatar_url = :avatar_url")
            params["avatar_url"] = avatar_url
        if full_name and not user.full_name:
            updates.append("full_name = :full_name")
            params["full_name"] = full_name

        if updates:
            db.execute(
                text(f"UPDATE users SET {', '.join(updates)} WHERE id = :id"),
                params,
            )

    # 6. Create session — commit BEFORE building the redirect response with the cookie
    token = create_app_session(user.id, db)
    db.commit()

    frontend = settings.FRONTEND_URL.rstrip("/")
    redirect_response = RedirectResponse(
        f"{frontend}/auth/google/callback", status_code=302
    )
    set_auth_cookie(redirect_response, token)
    return redirect_response
