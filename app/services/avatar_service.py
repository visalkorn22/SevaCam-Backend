# backend/app/services/avatar_service.py
import os
import tempfile
from pathlib import Path

from fastapi import UploadFile
from PIL import Image, ImageOps

_AVATARS_DIR = Path(__file__).resolve().parent.parent.parent / "uploads" / "avatars"
_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
_OUTPUT_SIZE = (256, 256)
_ALLOWED_TYPES = {"image/jpeg", "image/png", "image/webp"}


def _avatars_dir() -> Path:
    _AVATARS_DIR.mkdir(parents=True, exist_ok=True)
    return _AVATARS_DIR


async def save_avatar(user_id: str, file: UploadFile) -> str:
    """Validate, center-crop, resize to 256x256 WebP, atomically save.
    Returns the relative public path e.g. '/uploads/avatars/<user_id>.webp'.
    Raises ValueError with a short error_code string on validation failure.
    """
    # Sanitize user_id — strip any directory components
    safe_id = Path(user_id).name
    if not safe_id:
        raise ValueError("invalid_user_id")

    content_type = file.content_type or ""
    if content_type not in _ALLOWED_TYPES:
        raise ValueError("invalid_file_type")

    contents = await file.read()
    if len(contents) > _MAX_BYTES:
        raise ValueError("file_too_large")

    tmp_path: str | None = None
    out_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".tmp") as tmp:
            tmp.write(contents)
            tmp_path = tmp.name

        avatars = _avatars_dir()
        with Image.open(tmp_path) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")

            w, h = img.size
            side = min(w, h)
            left = (w - side) // 2
            top = (h - side) // 2
            img = img.crop((left, top, left + side, top + side))
            img = img.resize(_OUTPUT_SIZE, Image.LANCZOS)

            dest = avatars / f"{safe_id}.webp"
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".webp", dir=avatars
            ) as out_tmp:
                out_path = out_tmp.name
            img.save(out_path, format="WEBP", quality=85)
            os.replace(out_path, dest)
            out_path = None  # successfully transferred; do not delete
    except ValueError:
        raise
    except Exception:
        raise ValueError("invalid_image")
    finally:
        for p in (tmp_path, out_path):
            if p:
                try:
                    os.unlink(p)
                except Exception:
                    pass

    return f"/uploads/avatars/{safe_id}.webp"


def delete_avatar(user_id: str) -> None:
    """Delete the avatar file for user_id. Silent no-op if file does not exist."""
    safe_id = Path(user_id).name
    if not safe_id:
        return
    path = _avatars_dir() / f"{safe_id}.webp"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
