# backend/app/api/avatar.py
from typing import Any, Dict

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.auth import get_current_user
from app.core.database import get_db
from app.services.avatar_service import delete_avatar, save_avatar

router = APIRouter(prefix="/api/me", tags=["avatar"])


@router.post("/avatar")
async def upload_avatar(
    file: UploadFile = File(...),
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(current_user["id"])

    try:
        avatar_url = await save_avatar(user_id, file)
    except ValueError as exc:
        error_code = str(exc)
        messages = {
            "invalid_file_type": "Please upload a JPG, PNG, or WebP image.",
            "file_too_large": "File too large — max 5 MB.",
            "invalid_image": "File could not be read as an image.",
        }
        raise HTTPException(
            status_code=400,
            detail=messages.get(error_code, "Upload failed."),
        )

    try:
        db.execute(
            text("UPDATE users SET avatar_url = :url WHERE id = :id"),
            {"url": avatar_url, "id": user_id},
        )
        db.commit()
    except Exception:
        db.rollback()
        delete_avatar(user_id)
        raise HTTPException(status_code=500, detail="Could not save avatar.")

    return {"avatar_url": avatar_url}


@router.delete("/avatar")
def remove_avatar(
    current_user: Dict[str, Any] = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    user_id = str(current_user["id"])
    delete_avatar(user_id)
    db.execute(
        text("UPDATE users SET avatar_url = NULL WHERE id = :id"),
        {"id": user_id},
    )
    db.commit()
    return {"avatar_url": None}
