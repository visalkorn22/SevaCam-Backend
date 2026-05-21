from typing import Optional, Iterable, Dict, Any, Set

from fastapi import Depends, Header, Cookie, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text

from app.core.database import get_db

ADMIN_ROLES: Set[str] = {"admin", "superadmin"}
STAFF_ROLES: Set[str] = {"staff", "admin", "superadmin"}


def resolve_token(authorization: Optional[str], auth_token: Optional[str]) -> Optional[str]:
    if authorization and authorization.startswith("Bearer "):
        return authorization.replace("Bearer ", "")
    return auth_token


def get_user_by_token(db: Session, token: str):
    return db.execute(
        text(
            """
            SELECT u.id, u.email, u.full_name, u.role, u.phone, u.avatar_url,
                   u.timezone, u.is_active, u.email_verified, u.created_at
            FROM users u
            JOIN sessions s ON s.user_id = u.id
            WHERE s.token = :token AND s.expires_at > NOW()
            """
        ),
        {"token": token},
    ).fetchone()


def get_current_user(
    authorization: Optional[str] = Header(None),
    auth_token: Optional[str] = Cookie(None),
    db: Session = Depends(get_db),
) -> Dict[str, Any]:
    token = resolve_token(authorization, auth_token)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    user = get_user_by_token(db, token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session")

    user_dict = dict(user._mapping)
    if not user_dict.get("is_active", True):
        raise HTTPException(status_code=403, detail="Account is disabled")
    if not user_dict.get("email_verified", True):
        raise HTTPException(status_code=403, detail="Email not verified")

    return user_dict


def require_roles(*roles: Iterable[str]):
    allowed = {role for role in roles}

    def role_guard(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if current_user.get("role") not in allowed:
            raise HTTPException(status_code=403, detail="Forbidden")
        return current_user

    return role_guard


def is_admin(user: Dict[str, Any]) -> bool:
    return user.get("role") in ADMIN_ROLES


def is_staff(user: Dict[str, Any]) -> bool:
    return user.get("role") in STAFF_ROLES


def get_permissions_for_role(db: Session, role: str) -> Set[str]:
    result = db.execute(
        text(
            """
            SELECT p.code
            FROM role_permissions rp
            JOIN permissions p ON p.code = rp.permission_code
            WHERE rp.role_name = :role
            """
        ),
        {"role": role},
    )
    return {row[0] for row in result.fetchall()}


def require_permissions(*permissions: Iterable[str]):
    required = {perm for perm in permissions}

    def permission_guard(
        current_user: Dict[str, Any] = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Dict[str, Any]:
        role = current_user.get("role")
        if not role:
            raise HTTPException(status_code=403, detail="Forbidden")

        if role in ADMIN_ROLES:
            return current_user

        user_permissions = get_permissions_for_role(db, role)
        if not required.issubset(user_permissions):
            raise HTTPException(status_code=403, detail="Forbidden")

        return current_user

    return permission_guard
