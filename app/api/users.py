from datetime import datetime
from typing import Optional, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from sqlalchemy import text
import uuid

from app.core.database import get_db
from app.core.auth import get_current_user, require_roles, get_permissions_for_role

router = APIRouter()

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
DEFAULT_APP_TIMEZONE = "Asia/Phnom_Penh"


class UserResponse(BaseModel):
    id: str
    email: EmailStr
    full_name: Optional[str] = None
    role: str
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    timezone: Optional[str] = None
    is_active: bool
    created_at: datetime


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    phone: Optional[str] = None
    avatar_url: Optional[str] = None
    timezone: Optional[str] = None
    role: Optional[str] = None
    is_active: Optional[bool] = None


class UserStatusUpdate(BaseModel):
    is_active: bool


class AdminUserCreate(BaseModel):
    email: EmailStr
    password: str
    full_name: Optional[str] = None
    phone: Optional[str] = None
    timezone: Optional[str] = None
    role: str = "customer"
    is_active: Optional[bool] = True


def _serialize_user(row) -> dict:
    data = dict(row._mapping)
    if "id" in data and data["id"] is not None:
        data["id"] = str(data["id"])
    return data


def _ensure_self_or_admin(current_user: dict, user_id: str) -> None:
    if current_user.get("role") in {"admin", "superadmin"}:
        return
    if current_user.get("id") != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")


ROLE_TRANSITIONS = {
    "customer": {"staff"},
    "staff": {"customer", "admin"},
    "admin": {"superadmin"},
}


def _ensure_role_exists(db: Session, role: str) -> None:
    exists = db.execute(
        text("SELECT 1 FROM roles WHERE name = :role"),
        {"role": role},
    ).fetchone()
    if not exists:
        raise HTTPException(status_code=400, detail="Invalid role")


def _validate_role_change(
    db: Session,
    current_user: dict,
    user_id: str,
    current_role: str,
    new_role: str,
) -> None:
    if current_role == new_role:
        return

    allowed_next = ROLE_TRANSITIONS.get(current_role, set())
    if new_role not in allowed_next:
        raise HTTPException(status_code=400, detail="Invalid role transition")

    if new_role == "staff" and current_user.get("role") not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    if new_role == "customer" and current_user.get("role") not in {"admin", "superadmin"}:
        raise HTTPException(status_code=403, detail="Forbidden")

    if new_role in {"admin", "superadmin"} and current_user.get("role") != "superadmin":
        raise HTTPException(status_code=403, detail="Forbidden")

    if new_role == "admin":
        existing_admin = db.execute(
            text("SELECT id FROM users WHERE role = 'admin' AND id != :id"),
            {"id": user_id},
        ).fetchone()
        if existing_admin:
            raise HTTPException(status_code=409, detail="Admin already exists")

    if new_role == "superadmin":
        existing_superadmin = db.execute(
            text("SELECT id FROM users WHERE role = 'superadmin' AND id != :id"),
            {"id": user_id},
        ).fetchone()
        if existing_superadmin:
            raise HTTPException(status_code=409, detail="SuperAdmin already exists")

    if current_role == "superadmin" and new_role != "superadmin":
        remaining_superadmins = db.execute(
            text("SELECT COUNT(*) FROM users WHERE role = 'superadmin'"),
        ).fetchone()
        if remaining_superadmins and remaining_superadmins[0] <= 1:
            raise HTTPException(status_code=409, detail="Cannot remove the last SuperAdmin")


def _ensure_permission(db: Session, current_user: dict, permission: str) -> None:
    role = current_user.get("role")
    if not role:
        raise HTTPException(status_code=403, detail="Forbidden")

    permissions = get_permissions_for_role(db, role)
    if permission not in permissions:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.get("/users", response_model=List[UserResponse])
def list_users(
    search: Optional[str] = None,
    role: Optional[str] = None,
    is_active: Optional[bool] = None,
    skip: int = 0,
    limit: int = 100,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_permission(db, current_user, "staff:manage")
    query = """
        SELECT id, email, full_name, role, phone, avatar_url, timezone, is_active, created_at
        FROM users
        WHERE 1=1
    """
    params = {"skip": skip, "limit": limit}

    if role:
        query += " AND role = :role"
        params["role"] = role
    if is_active is not None:
        query += " AND is_active = :is_active"
        params["is_active"] = is_active
    if search:
        query += " AND (LOWER(full_name) LIKE :search OR LOWER(email) LIKE :search OR LOWER(phone) LIKE :search)"
        params["search"] = f"%{search.lower()}%"

    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :skip"

    result = db.execute(text(query), params)
    return [_serialize_user(row) for row in result.fetchall()]


@router.post("/users", response_model=UserResponse)
def create_user(
    payload: AdminUserCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_permission(db, current_user, "staff:manage")
    if len(payload.password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    _ensure_role_exists(db, payload.role)
    if payload.role in {"admin", "superadmin"}:
        if current_user.get("role") != "superadmin":
            raise HTTPException(status_code=403, detail="Forbidden")
        if payload.role == "admin":
            existing_admin = db.execute(
                text("SELECT id FROM users WHERE role = 'admin'"),
            ).fetchone()
            if existing_admin:
                raise HTTPException(status_code=409, detail="Admin already exists")
        if payload.role == "superadmin":
            existing_superadmin = db.execute(
                text("SELECT id FROM users WHERE role = 'superadmin'"),
            ).fetchone()
            if existing_superadmin:
                raise HTTPException(status_code=409, detail="SuperAdmin already exists")

    exists = db.execute(
        text("SELECT 1 FROM users WHERE email = :email"),
        {"email": payload.email},
    ).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail="Email already exists")

    password_hash = pwd_context.hash(payload.password)
    user_id = str(uuid.uuid4())

    created = db.execute(
        text(
            """
            INSERT INTO users (id, email, full_name, role, phone, timezone, password_hash, is_active)
            VALUES (:id, :email, :full_name, :role, :phone, :timezone, :password_hash, :is_active)
            RETURNING id, email, full_name, role, phone, avatar_url, timezone, is_active, created_at
            """
        ),
        {
            "id": user_id,
            "email": payload.email,
            "full_name": payload.full_name,
            "role": payload.role,
            "phone": payload.phone,
            "timezone": payload.timezone or DEFAULT_APP_TIMEZONE,
            "password_hash": password_hash,
            "is_active": payload.is_active if payload.is_active is not None else True,
        },
    ).fetchone()
    db.commit()

    return _serialize_user(created)


@router.get("/users/{user_id}", response_model=UserResponse)
def get_user(
    user_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_self_or_admin(current_user, user_id)

    result = db.execute(
        text(
            """
            SELECT id, email, full_name, role, phone, avatar_url, timezone, is_active, created_at
            FROM users
            WHERE id = :id
            """
        ),
        {"id": user_id},
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="User not found")

    return _serialize_user(result)


@router.patch("/users/{user_id}", response_model=UserResponse)
def update_user(
    user_id: str,
    payload: UserUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if not user_id or user_id == "undefined":
        raise HTTPException(status_code=400, detail="Invalid user id")
    if not payload.model_dump(exclude_unset=True):
        raise HTTPException(status_code=400, detail="At least one field is required")
    is_admin = current_user.get("role") in {"admin", "superadmin"}
    _ensure_self_or_admin(current_user, user_id)

    if is_admin and current_user.get("id") != user_id:
        _ensure_permission(db, current_user, "staff:manage")

    if payload.role is not None and current_user.get("id") == user_id:
        raise HTTPException(status_code=403, detail="You cannot change your own role")

    if not is_admin and (payload.role is not None or payload.is_active is not None):
        raise HTTPException(status_code=403, detail="Forbidden")

    target = db.execute(
        text("SELECT id, role FROM users WHERE id = :id"),
        {"id": user_id},
    ).fetchone()
    if not target:
        raise HTTPException(status_code=404, detail="User not found")

    if is_admin and payload.role is not None:
        _ensure_permission(db, current_user, "roles:assign")
        _ensure_role_exists(db, payload.role)
        _validate_role_change(
            db,
            current_user=current_user,
            user_id=user_id,
            current_role=target.role,
            new_role=payload.role,
        )

    updates = []
    params = {"id": user_id}

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
        params["timezone"] = payload.timezone
    if is_admin and payload.role is not None:
        updates.append("role = :role")
        params["role"] = payload.role
    if is_admin and payload.is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = payload.is_active

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    query = f"""
        UPDATE users
        SET {", ".join(updates)}
        WHERE id = :id
        RETURNING id, email, full_name, role, phone, avatar_url, timezone, is_active, created_at
    """
    updated = db.execute(text(query), params).fetchone()
    db.commit()

    return _serialize_user(updated)


@router.patch("/users/{user_id}/status", response_model=UserResponse)
def update_user_status(
    user_id: str,
    payload: UserStatusUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    _ensure_permission(db, current_user, "staff:manage")
    updated = db.execute(
        text(
            """
            UPDATE users
            SET is_active = :is_active
            WHERE id = :id
            RETURNING id, email, full_name, role, phone, avatar_url, timezone, is_active, created_at
            """
        ),
        {"id": user_id, "is_active": payload.is_active},
    ).fetchone()
    db.commit()

    if not updated:
        raise HTTPException(status_code=404, detail="User not found")

    return _serialize_user(updated)
