import base64
import hashlib
import hmac as hmac_module
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ["SECRET_KEY"] = "test-secret-key-for-testing"
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/auth/google/callback")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")
os.environ.setdefault("SESSION_DAYS", "30")
os.environ.setdefault("COOKIE_NAME", "auth_token")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("COOKIE_SAMESITE", "lax")
os.environ.setdefault("COOKIE_PATH", "/")

from app.api.google_oauth import _make_state, _verify_state, _decode_state_payload_unsafe


def _build_state(nonce: str, mode: str, exp: int, secret: str = "test-secret-key-for-testing") -> str:
    """Helper to build a state with a specific exp — for testing expiry."""
    payload = json.dumps({"nonce": nonce, "mode": mode, "exp": exp})
    b64 = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
    sig = hmac_module.new(secret.encode(), b64.encode(), hashlib.sha256).hexdigest()
    return f"{b64}.{sig}"


# ── _make_state ────────────────────────────────────────────────────────────────

def test_make_state_has_two_dot_separated_parts():
    state = _make_state("login")
    parts = state.rsplit(".", 1)
    assert len(parts) == 2


def test_make_state_payload_contains_mode():
    state = _make_state("signup")
    b64 = state.rsplit(".", 1)[0]
    padded = b64 + "=" * (4 - len(b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    assert payload["mode"] == "signup"


def test_make_state_payload_contains_nonce():
    state = _make_state("login")
    b64 = state.rsplit(".", 1)[0]
    padded = b64 + "=" * (4 - len(b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded).decode())
    assert "nonce" in payload
    assert len(payload["nonce"]) > 20


def test_make_state_nonces_are_unique():
    s1 = _make_state("login")
    s2 = _make_state("login")
    assert s1 != s2


# ── _verify_state ──────────────────────────────────────────────────────────────

def test_verify_state_returns_correct_mode():
    state = _make_state("signup")
    payload = _verify_state(state)
    assert payload["mode"] == "signup"


def test_verify_state_returns_nonce():
    state = _make_state("login")
    payload = _verify_state(state)
    assert "nonce" in payload
    assert len(payload["nonce"]) > 20


def test_verify_state_rejects_tampered_signature():
    state = _make_state("login")
    b64, _ = state.rsplit(".", 1)
    tampered = f"{b64}.{'a' * 64}"
    with pytest.raises(ValueError, match="signature"):
        _verify_state(tampered)


def test_verify_state_rejects_expired():
    state = _build_state("nonce-abc", "login", exp=int(time.time()) - 10)
    with pytest.raises(ValueError, match="expired"):
        _verify_state(state)


def test_verify_state_rejects_malformed_no_dot():
    with pytest.raises(ValueError, match="Malformed"):
        _verify_state("nodothere")


def test_verify_state_rejects_malformed_payload():
    b64 = base64.urlsafe_b64encode(b"not-json").decode().rstrip("=")
    sig = hmac_module.new("test-secret-key-for-testing".encode(), b64.encode(), hashlib.sha256).hexdigest()
    state = f"{b64}.{sig}"
    with pytest.raises(ValueError):
        _verify_state(state)


# ── _decode_state_payload_unsafe ───────────────────────────────────────────────

def test_decode_state_payload_unsafe_returns_nonce():
    state = _make_state("login")
    payload = _decode_state_payload_unsafe(state)
    assert "nonce" in payload
    assert "mode" in payload


# ── /google/start ──────────────────────────────────────────────────────────────

from fastapi import FastAPI
from fastapi.testclient import TestClient


def make_google_app():
    from app.api.google_oauth import router
    app = FastAPI()
    app.include_router(router)
    return app


def test_start_redirects_to_google_when_configured():
    client = TestClient(make_google_app(), follow_redirects=False)
    response = client.get("/api/auth/google/start?mode=login")
    assert response.status_code in (302, 307)
    location = response.headers["location"]
    assert "accounts.google.com" in location
    assert "test-client-id" in location
    assert "scope=openid" in location
    assert "state=" in location
    assert "nonce=" in location
    assert "access_type" not in location  # must not request refresh tokens


def test_start_redirects_to_error_when_client_id_missing():
    from unittest.mock import patch
    with patch("app.api.google_oauth.settings") as m:
        m.GOOGLE_CLIENT_ID = None
        m.GOOGLE_CLIENT_SECRET = "secret"
        m.FRONTEND_URL = "http://localhost:3000"
        client = TestClient(make_google_app(), follow_redirects=False)
        response = client.get("/api/auth/google/start")
    assert response.status_code in (302, 307)
    assert "oauth_not_configured" in response.headers["location"]


def test_start_defaults_mode_to_login():
    client = TestClient(make_google_app(), follow_redirects=False)
    response = client.get("/api/auth/google/start")
    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]


def test_start_invalid_mode_falls_back_to_login():
    client = TestClient(make_google_app(), follow_redirects=False)
    response = client.get("/api/auth/google/start?mode=invalid")
    assert response.status_code in (302, 307)
    assert "accounts.google.com" in response.headers["location"]


# ── /google/callback ───────────────────────────────────────────────────────────

from unittest.mock import MagicMock, patch


class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)


def make_db_new_user():
    """Mock db for the new-user path: lookup→None, role→exists, insert→user, session→ok."""
    db = MagicMock()
    new_user = FakeRow(
        id="new-user-id", email="new@example.com", full_name="New User",
        role="customer", phone=None, avatar_url="https://pic.url",
        timezone="Asia/Phnom_Penh", is_active=True, email_verified=True,
    )
    db.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=None)),           # user lookup → None
        MagicMock(fetchone=MagicMock(return_value=FakeRow(name="customer"))),  # role check
        MagicMock(fetchone=MagicMock(return_value=new_user)),       # INSERT RETURNING
        MagicMock(),                                                 # session INSERT
    ]
    return db


def make_db_existing_user(user: FakeRow):
    """Mock db for the existing-user path: lookup→user, then session INSERT."""
    db = MagicMock()
    db.execute.side_effect = [
        MagicMock(fetchone=MagicMock(return_value=user)),  # user lookup
        MagicMock(),                                        # session INSERT
    ]
    return db


def make_callback_app(db):
    from app.core.database import get_db
    from app.api.google_oauth import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    return app


def make_valid_state(mode: str = "login") -> str:
    return _make_state(mode)


def test_callback_missing_code_redirects_state_invalid():
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    state = make_valid_state()
    response = client.get(f"/api/auth/google/callback?state={state}")
    assert response.status_code in (302, 307)
    assert "state_invalid" in response.headers["location"]


def test_callback_missing_state_redirects_state_invalid():
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    response = client.get("/api/auth/google/callback?code=abc")
    assert response.status_code in (302, 307)
    assert "state_invalid" in response.headers["location"]


def test_callback_invalid_state_signature_redirects_error():
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    response = client.get("/api/auth/google/callback?code=abc&state=bad.state")
    assert response.status_code in (302, 307)
    assert "state_invalid" in response.headers["location"]


def test_callback_google_error_param_redirects_state_invalid():
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    state = make_valid_state()
    response = client.get(f"/api/auth/google/callback?error=access_denied&state={state}")
    assert response.status_code in (302, 307)
    assert "state_invalid" in response.headers["location"]


def test_callback_code_exchange_failure_redirects_google_failed():
    state = make_valid_state()
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    with patch("app.api.google_oauth._exchange_code", side_effect=ValueError("exchange failed")):
        response = client.get(f"/api/auth/google/callback?code=bad-code&state={state}")
    assert response.status_code in (302, 307)
    assert "google_failed" in response.headers["location"]


def test_callback_id_token_verification_failure_redirects_google_failed():
    state = make_valid_state()
    db = MagicMock()
    client = TestClient(make_callback_app(db), follow_redirects=False)
    with patch("app.api.google_oauth._exchange_code", return_value={"access_token": "acc", "id_token": "id"}), \
         patch("app.api.google_oauth._verify_google_id_token", side_effect=ValueError("bad jwt")):
        response = client.get(f"/api/auth/google/callback?code=code&state={state}")
    assert response.status_code in (302, 307)
    assert "google_failed" in response.headers["location"]


def test_callback_new_user_happy_path_sets_cookie_and_redirects():
    state = make_valid_state("login")
    db = make_db_new_user()
    client = TestClient(make_callback_app(db), follow_redirects=False)

    with patch("app.api.google_oauth._exchange_code", return_value={"access_token": "acc", "id_token": "idt"}), \
         patch("app.api.google_oauth._verify_google_id_token", return_value={
             "email": "new@example.com", "email_verified": True, "sub": "google-123"
         }), \
         patch("app.api.google_oauth._get_userinfo", return_value={"name": "New User", "picture": "https://pic.url"}):
        response = client.get(f"/api/auth/google/callback?code=code&state={state}")

    assert response.status_code in (302, 307)
    assert "auth/google/callback" in response.headers["location"]
    assert "auth_token" in response.headers.get("set-cookie", "")


def test_callback_inactive_user_redirects_account_inactive():
    state = make_valid_state()
    inactive_user = FakeRow(
        id="u1", email="x@x.com", full_name="X", role="customer",
        phone=None, avatar_url=None, timezone="UTC",
        is_active=False, email_verified=True,
    )
    db = make_db_existing_user(inactive_user)
    client = TestClient(make_callback_app(db), follow_redirects=False)

    with patch("app.api.google_oauth._exchange_code", return_value={"access_token": "acc", "id_token": "idt"}), \
         patch("app.api.google_oauth._verify_google_id_token", return_value={
             "email": "x@x.com", "email_verified": True, "sub": "g-sub"
         }), \
         patch("app.api.google_oauth._get_userinfo", return_value={}):
        response = client.get(f"/api/auth/google/callback?code=code&state={state}")

    assert response.status_code in (302, 307)
    assert "account_inactive" in response.headers["location"]
