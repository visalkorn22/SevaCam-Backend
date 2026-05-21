# backend/tests/test_avatar_api.py
import io
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test")
os.environ.setdefault("FRONTEND_URL", "http://localhost:3000")
os.environ.setdefault("SESSION_DAYS", "30")
os.environ.setdefault("COOKIE_NAME", "auth_token")
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("COOKIE_SAMESITE", "lax")
os.environ.setdefault("COOKIE_PATH", "/")


FAKE_USER = {
    "id": "user-test-123",
    "email": "test@example.com",
    "full_name": "Test User",
    "role": "customer",
    "is_active": True,
    "email_verified": True,
}


def _make_image_bytes(size=(400, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(100, 150, 200)).save(buf, format="JPEG")
    return buf.getvalue()


def make_db():
    db = MagicMock()
    db.execute.return_value = MagicMock()
    return db


def make_app(db):
    from app.api.avatar import router
    from app.core.auth import get_current_user
    from app.core.database import get_db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: FAKE_USER
    return app


def test_upload_avatar_happy_path(tmp_path):
    from app.services import avatar_service
    db = make_db()
    client = TestClient(make_app(db))

    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.post(
            "/api/me/avatar",
            files={"file": ("photo.jpg", _make_image_bytes(), "image/jpeg")},
        )

    assert resp.status_code == 200
    assert resp.json()["avatar_url"] == "/uploads/avatars/user-test-123.webp"
    db.execute.assert_called()
    db.commit.assert_called_once()


def test_upload_avatar_invalid_type_returns_400(tmp_path):
    from app.services import avatar_service
    db = make_db()
    client = TestClient(make_app(db))

    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.post(
            "/api/me/avatar",
            files={"file": ("doc.pdf", b"data", "application/pdf")},
        )

    assert resp.status_code == 400
    assert "JPG" in resp.json()["detail"] or "PNG" in resp.json()["detail"]


def test_upload_avatar_too_large_returns_400(tmp_path):
    from app.services import avatar_service
    db = make_db()
    client = TestClient(make_app(db))

    big = b"x" * (6 * 1024 * 1024)
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.post(
            "/api/me/avatar",
            files={"file": ("big.jpg", big, "image/jpeg")},
        )

    assert resp.status_code == 400
    assert "5 MB" in resp.json()["detail"]


def test_delete_avatar_returns_null(tmp_path):
    from app.services import avatar_service
    db = make_db()
    client = TestClient(make_app(db))

    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.delete("/api/me/avatar")

    assert resp.status_code == 200
    assert resp.json()["avatar_url"] is None
    db.execute.assert_called()
    db.commit.assert_called_once()


def test_delete_avatar_removes_file(tmp_path):
    from app.services import avatar_service

    (tmp_path / "user-test-123.webp").write_bytes(b"fake")

    db = make_db()
    client = TestClient(make_app(db))

    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.delete("/api/me/avatar")

    assert resp.status_code == 200
    assert not (tmp_path / "user-test-123.webp").exists()


def test_upload_avatar_corrupt_file_returns_400(tmp_path):
    from app.services import avatar_service
    db = make_db()
    client = TestClient(make_app(db))

    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        resp = client.post(
            "/api/me/avatar",
            files={"file": ("corrupt.jpg", b"this is not an image", "image/jpeg")},
        )

    assert resp.status_code == 400
    assert "read as an image" in resp.json()["detail"]
    db.execute.assert_not_called()


def test_upload_avatar_missing_file_returns_422():
    db = make_db()
    client = TestClient(make_app(db))
    resp = client.post("/api/me/avatar")
    assert resp.status_code == 422
    db.execute.assert_not_called()
