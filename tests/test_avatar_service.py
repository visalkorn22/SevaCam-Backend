# backend/tests/test_avatar_service.py
import io
import os
import sys
import asyncio

import pytest
from PIL import Image
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("SECRET_KEY", "test-secret")


def _make_upload_file(content: bytes, content_type: str, filename: str = "test.jpg"):
    f = MagicMock()
    f.content_type = content_type
    f.filename = filename
    f.read = AsyncMock(return_value=content)
    return f


def _make_image_bytes(mode="RGB", size=(400, 300), fmt="JPEG") -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color=(100, 150, 200)).save(buf, format=fmt)
    return buf.getvalue()


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_save_avatar_returns_relative_path(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(_make_image_bytes(), "image/jpeg")
        result = run(avatar_service.save_avatar("user-123", f))
    assert result == "/uploads/avatars/user-123.webp"


def test_save_avatar_creates_webp_file(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(_make_image_bytes(), "image/jpeg")
        run(avatar_service.save_avatar("user-abc", f))
    out = tmp_path / "user-abc.webp"
    assert out.exists()
    with Image.open(out) as img:
        assert img.size == (256, 256)
        assert img.format == "WEBP"


def test_save_avatar_rejects_invalid_content_type(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(b"data", "application/pdf")
        with pytest.raises(ValueError, match="invalid_file_type"):
            run(avatar_service.save_avatar("u1", f))


def test_save_avatar_rejects_oversized_file(tmp_path):
    from app.services import avatar_service
    big = b"x" * (6 * 1024 * 1024)
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(big, "image/jpeg")
        with pytest.raises(ValueError, match="file_too_large"):
            run(avatar_service.save_avatar("u1", f))


def test_save_avatar_rejects_spoofed_file(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(b"this is not an image", "image/jpeg")
        with pytest.raises(ValueError, match="invalid_image"):
            run(avatar_service.save_avatar("u1", f))


def test_save_avatar_overwrites_existing(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        for _ in range(2):
            f = _make_upload_file(_make_image_bytes(size=(500, 500)), "image/jpeg")
            run(avatar_service.save_avatar("user-ow", f))
    files = list(tmp_path.glob("*.webp"))
    assert len(files) == 1


def test_delete_avatar_removes_file(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        f = _make_upload_file(_make_image_bytes(), "image/jpeg")
        run(avatar_service.save_avatar("user-del", f))
        avatar_service.delete_avatar("user-del")
    assert not (tmp_path / "user-del.webp").exists()


def test_delete_avatar_silent_when_missing(tmp_path):
    from app.services import avatar_service
    with patch.object(avatar_service, "_AVATARS_DIR", tmp_path):
        avatar_service.delete_avatar("nonexistent-user")  # must not raise
