import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SESSION_DAYS", "30")
os.environ.setdefault("COOKIE_NAME", "auth_token")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("COOKIE_SAMESITE", "lax")
os.environ.setdefault("COOKIE_PATH", "/")

from app.core.session import create_app_session, set_auth_cookie
from fastapi.responses import JSONResponse


def make_db():
    db = MagicMock()
    db.execute.return_value = MagicMock()
    return db


def test_create_app_session_returns_64_char_hex_token():
    token = create_app_session("user-id-123", make_db())
    assert isinstance(token, str)
    assert len(token) == 64  # secrets.token_hex(32) = 64 hex chars


def test_create_app_session_inserts_with_correct_user_id():
    db = make_db()
    create_app_session("user-abc", db)
    call_args = db.execute.call_args[0][1]
    assert call_args["user_id"] == "user-abc"


def test_create_app_session_tokens_are_unique():
    t1 = create_app_session("u1", make_db())
    t2 = create_app_session("u2", make_db())
    assert t1 != t2


def test_create_app_session_does_not_commit():
    db = make_db()
    create_app_session("u1", db)
    db.commit.assert_not_called()


def test_set_auth_cookie_sets_httponly():
    response = JSONResponse(content={})
    set_auth_cookie(response, "test-token")
    cookie_header = response.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie_header


def test_set_auth_cookie_includes_token_value():
    response = JSONResponse(content={})
    set_auth_cookie(response, "my-session-token")
    assert "my-session-token" in response.headers.get("set-cookie", "")


def test_set_auth_cookie_uses_configured_cookie_name():
    response = JSONResponse(content={})
    set_auth_cookie(response, "tok")
    assert "auth_token" in response.headers.get("set-cookie", "")
