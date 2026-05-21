from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.core.database import get_db
from app.core.auth import get_current_user, require_permissions, require_roles, is_admin
from app.core.audit import log_audit
from app.models.schemas import (
    StaffServiceCreate,
    StaffServiceResponse,
    StaffServiceUpdate,
    StaffServiceOverrideCreate,
    StaffServiceOverrideUpdate,
    StaffServiceOverrideResponse,
)
from app.core.config import settings
import uuid

router = APIRouter()

def _ensure_staff_or_admin(current_user: dict, staff_id: str) -> None:
    if is_admin(current_user):
        return
    if current_user.get("id") != staff_id:
        raise HTTPException(status_code=403, detail="Forbidden")

def _normalize_uuid_fields(row: dict, fields: list[str]) -> dict:
    for field in fields:
        if row.get(field) is not None:
            row[field] = str(row[field])
    return row

@router.get("/dashboard")
async def staff_dashboard(
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Staff dashboard summary (staff/admin only)"""
    if settings.FEATURE_SET != "full":
        return {
            "todayBookings": [],
            "upcomingBookings": [],
            "totalRevenue": 0.0,
            "totalBookings": 0,
        }
    staff_id = current_user.get("id")
    try:
        staff_timezone = ZoneInfo(current_user.get("timezone") or "Asia/Phnom_Penh")
    except ZoneInfoNotFoundError:
        staff_timezone = dt_timezone.utc

    today_local = datetime.now(staff_timezone).date()
    day_start_local = datetime.combine(today_local, time(0, 0), tzinfo=staff_timezone)
    day_end_local = day_start_local + timedelta(days=1)
    day_start_utc = day_start_local.astimezone(dt_timezone.utc)
    day_end_utc = day_end_local.astimezone(dt_timezone.utc)

    today_result = db.execute(
        text(
            """
        SELECT b.id, b.start_time_utc, b.status,
               s.name as service_name, s.duration_minutes, s.price,
               c.full_name as customer_name, c.phone as customer_phone, c.email as customer_email
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN customers c ON b.customer_id = c.id
        WHERE b.staff_id = :staff_id
          AND b.start_time_utc >= :day_start_utc
          AND b.start_time_utc < :day_end_utc
        ORDER BY b.start_time_utc ASC
        """
        ),
        {
            "staff_id": staff_id,
            "day_start_utc": day_start_utc,
            "day_end_utc": day_end_utc,
        },
    )

    upcoming_result = db.execute(
        text(
            """
        SELECT b.id, b.start_time_utc, b.status,
               s.name as service_name, s.duration_minutes, s.price,
               c.full_name as customer_name, c.phone as customer_phone, c.email as customer_email
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN customers c ON b.customer_id = c.id
        WHERE b.staff_id = :staff_id
          AND b.start_time_utc > NOW()
        ORDER BY b.start_time_utc ASC
        LIMIT 20
        """
        ),
        {"staff_id": staff_id},
    )

    stats_result = db.execute(
        text(
            """
        SELECT COUNT(*) as total_bookings,
               COALESCE(SUM(CASE WHEN b.payment_status = 'paid' THEN s.price ELSE 0 END), 0) as total_revenue
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        WHERE b.staff_id = :staff_id
        """
        ),
        {"staff_id": staff_id},
    ).fetchone()

    def format_booking(row):
        return {
            "id": row[0],
            "start_time_utc": row[1],
            "status": row[2],
            "services": {
                "name": row[3],
                "duration_minutes": row[4],
                "price": row[5],
            },
            "customers": {
                "full_name": row[6],
                "phone": row[7],
                "email": row[8],
            },
        }

    return {
        "todayBookings": [format_booking(row) for row in today_result.fetchall()],
        "upcomingBookings": [format_booking(row) for row in upcoming_result.fetchall()],
        "totalRevenue": float(stats_result[1] or 0),
        "totalBookings": int(stats_result[0] or 0),
    }

@router.post("/services", response_model=StaffServiceResponse)
async def assign_staff_to_service(
    assignment: StaffServiceCreate,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db)
):
    """Assign a staff member to a service (Admin only)"""
    assignment_id = str(uuid.uuid4())

    staff_exists = db.execute(
        text("SELECT 1 FROM users WHERE id = :id"),
        {"id": assignment.staff_id},
    ).fetchone()
    if not staff_exists:
        raise HTTPException(status_code=400, detail="Invalid staff_id")

    service_exists = db.execute(
        text("SELECT 1 FROM services WHERE id = :id"),
        {"id": assignment.service_id},
    ).fetchone()
    if not service_exists:
        raise HTTPException(status_code=400, detail="Invalid service_id")

    try:
        result = db.execute(
            text(
                """
                INSERT INTO staff_services (
                    id, staff_id, service_id,
                    price_override, deposit_override, duration_override, buffer_override, capacity_override,
                    is_bookable, is_temporarily_unavailable, admin_only
                )
                VALUES (
                    :id, :staff_id, :service_id,
                    :price_override, :deposit_override, :duration_override, :buffer_override, :capacity_override,
                    :is_bookable, :is_temporarily_unavailable, :admin_only
                )
                ON CONFLICT (staff_id, service_id) DO UPDATE
                SET price_override = EXCLUDED.price_override,
                    deposit_override = EXCLUDED.deposit_override,
                    duration_override = EXCLUDED.duration_override,
                    buffer_override = EXCLUDED.buffer_override,
                    capacity_override = EXCLUDED.capacity_override,
                    is_bookable = EXCLUDED.is_bookable,
                    is_temporarily_unavailable = EXCLUDED.is_temporarily_unavailable,
                    admin_only = EXCLUDED.admin_only
                RETURNING *
                """
            ),
            {
                "id": assignment_id,
                "staff_id": assignment.staff_id,
                "service_id": assignment.service_id,
                "price_override": assignment.price_override,
                "deposit_override": assignment.deposit_override,
                "duration_override": assignment.duration_override,
                "buffer_override": assignment.buffer_override,
                "capacity_override": assignment.capacity_override,
                "is_bookable": assignment.is_bookable,
                "is_temporarily_unavailable": assignment.is_temporarily_unavailable,
                "admin_only": assignment.admin_only,
            }
        ).fetchone()
        log_audit(
            db,
            current_user.get("id"),
            "create",
            "staff_service_assignment",
            assignment_id,
            assignment.model_dump(),
        )
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Unable to assign staff to service")

    return _normalize_uuid_fields(
        dict(result._mapping),
        ["id", "staff_id", "service_id"],
    )

@router.put("/services/{assignment_id}", response_model=StaffServiceResponse)
async def update_staff_service_assignment(
    assignment_id: str,
    payload: StaffServiceUpdate,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db),
):
    """Update staff service overrides/visibility (Admin only)"""
    updates = []
    params = {"id": assignment_id}

    if payload.price_override is not None:
        updates.append("price_override = :price_override")
        params["price_override"] = payload.price_override
    if payload.deposit_override is not None:
        updates.append("deposit_override = :deposit_override")
        params["deposit_override"] = payload.deposit_override
    if payload.duration_override is not None:
        updates.append("duration_override = :duration_override")
        params["duration_override"] = payload.duration_override
    if payload.buffer_override is not None:
        updates.append("buffer_override = :buffer_override")
        params["buffer_override"] = payload.buffer_override
    if payload.capacity_override is not None:
        updates.append("capacity_override = :capacity_override")
        params["capacity_override"] = payload.capacity_override
    if payload.is_bookable is not None:
        updates.append("is_bookable = :is_bookable")
        params["is_bookable"] = payload.is_bookable
    if payload.is_temporarily_unavailable is not None:
        updates.append("is_temporarily_unavailable = :is_temporarily_unavailable")
        params["is_temporarily_unavailable"] = payload.is_temporarily_unavailable
    if payload.admin_only is not None:
        updates.append("admin_only = :admin_only")
        params["admin_only"] = payload.admin_only

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = db.execute(
        text(f"UPDATE staff_services SET {', '.join(updates)} WHERE id = :id"),
        params,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Assignment not found")

    updated = db.execute(
        text("SELECT * FROM staff_services WHERE id = :id"),
        {"id": assignment_id},
    ).fetchone()
    return _normalize_uuid_fields(
        dict(updated._mapping),
        ["id", "staff_id", "service_id"],
    )

@router.get("/services/{staff_id}", response_model=List[dict])
async def get_staff_services(
    staff_id: str,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Get all services assigned to a staff member"""
    _ensure_staff_or_admin(current_user, staff_id)
    result = db.execute(
        text(
            """
            SELECT s.*, ss.id as assignment_id,
                   ss.price_override, ss.deposit_override, ss.duration_override,
                   ss.buffer_override, ss.capacity_override,
                   ss.is_bookable, ss.is_temporarily_unavailable, ss.admin_only
            FROM services s
            JOIN staff_services ss ON s.id = ss.service_id
            WHERE ss.staff_id = :staff_id
              AND s.is_active = TRUE
              AND s.is_archived = FALSE
              AND NOT (s.paused_from IS NOT NULL
                       AND s.paused_from <= NOW()
                       AND (s.paused_until IS NULL OR s.paused_until >= NOW()))
            """
        ),
        {"staff_id": staff_id}
    )
    
    services = result.fetchall()
    return [
        _normalize_uuid_fields(dict(row._mapping), ["id", "assignment_id"])
        for row in services
    ]

@router.delete("/services/{assignment_id}")
async def remove_staff_from_service(
    assignment_id: str,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db),
):
    """Remove a staff member from a service (Admin only)"""
    result = db.execute(
        text("DELETE FROM staff_services WHERE id = :id"),
        {"id": assignment_id}
    )
    db.commit()
    
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Assignment not found")
    
    return {"message": "Staff removed from service"}

@router.get("/{service_id}/staff", response_model=List[dict])
async def get_service_staff(service_id: str, db: Session = Depends(get_db)):
    """Get all staff members assigned to a service"""
    result = db.execute(
        text(
            """
            SELECT u.id, u.full_name, u.phone, u.avatar_url, u.role, ss.id as assignment_id,
                   ss.price_override, ss.deposit_override, ss.duration_override,
                   ss.buffer_override, ss.capacity_override,
                   ss.is_bookable, ss.is_temporarily_unavailable, ss.admin_only
            FROM users u
            JOIN staff_services ss ON u.id = ss.staff_id
            WHERE ss.service_id = :service_id AND u.is_active = TRUE
            """
        ),
        {"service_id": service_id}
    )
    
    staff = result.fetchall()
    return [
        {
            "id": str(row[0]) if row[0] is not None else None,
            "full_name": row[1],
            "phone": row[2],
            "avatar_url": row[3],
            "role": row[4],
            "assignment_id": str(row[5]) if row[5] is not None else None,
            "price_override": row[6],
            "deposit_override": row[7],
            "duration_override": row[8],
            "buffer_override": row[9],
            "capacity_override": row[10],
            "is_bookable": row[11],
            "is_temporarily_unavailable": row[12],
            "admin_only": row[13],
        }
        for row in staff
    ]

@router.post("/overrides", response_model=StaffServiceOverrideResponse)
async def create_staff_service_override(
    payload: StaffServiceOverrideCreate,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db),
):
    """Create staff service override (Admin only)."""
    override_id = str(uuid.uuid4())
    try:
        db.execute(
            text(
                """
                INSERT INTO staff_service_overrides (
                    id, staff_id, service_id, price_override, deposit_override,
                    duration_override, buffer_override, capacity_override, is_bookable
                )
                VALUES (
                    :id, :staff_id, :service_id, :price_override, :deposit_override,
                    :duration_override, :buffer_override, :capacity_override, :is_bookable
                )
                """
            ),
            {
                "id": override_id,
                "staff_id": payload.staff_id,
                "service_id": payload.service_id,
                "price_override": payload.price_override,
                "deposit_override": payload.deposit_override,
                "duration_override": payload.duration_override,
                "buffer_override": payload.buffer_override,
                "capacity_override": payload.capacity_override,
                "is_bookable": payload.is_bookable,
            },
        )
        db.commit()
    except Exception:
        db.rollback()
        raise HTTPException(status_code=400, detail="Override already exists or invalid IDs")

    created = db.execute(
        text("SELECT * FROM staff_service_overrides WHERE id = :id"),
        {"id": override_id},
    ).fetchone()
    return _normalize_uuid_fields(
        dict(created._mapping),
        ["id", "staff_id", "service_id"],
    )

@router.put("/overrides/{override_id}", response_model=StaffServiceOverrideResponse)
async def update_staff_service_override(
    override_id: str,
    payload: StaffServiceOverrideUpdate,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db),
):
    """Update staff service override (Admin only)."""
    updates = []
    params = {"id": override_id}

    if payload.price_override is not None:
        updates.append("price_override = :price_override")
        params["price_override"] = payload.price_override
    if payload.deposit_override is not None:
        updates.append("deposit_override = :deposit_override")
        params["deposit_override"] = payload.deposit_override
    if payload.duration_override is not None:
        updates.append("duration_override = :duration_override")
        params["duration_override"] = payload.duration_override
    if payload.buffer_override is not None:
        updates.append("buffer_override = :buffer_override")
        params["buffer_override"] = payload.buffer_override
    if payload.capacity_override is not None:
        updates.append("capacity_override = :capacity_override")
        params["capacity_override"] = payload.capacity_override
    if payload.is_bookable is not None:
        updates.append("is_bookable = :is_bookable")
        params["is_bookable"] = payload.is_bookable

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = db.execute(
        text(f"UPDATE staff_service_overrides SET {', '.join(updates)} WHERE id = :id"),
        params,
    )
    log_audit(
        db,
        current_user.get("id"),
        "update",
        "staff_service_override",
        override_id,
        payload.model_dump(),
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Override not found")

    updated = db.execute(
        text("SELECT * FROM staff_service_overrides WHERE id = :id"),
        {"id": override_id},
    ).fetchone()
    return _normalize_uuid_fields(
        dict(updated._mapping),
        ["id", "staff_id", "service_id"],
    )

@router.get("/overrides/{staff_id}", response_model=List[StaffServiceOverrideResponse])
async def list_staff_service_overrides(
    staff_id: str,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """List overrides for a staff member."""
    _ensure_staff_or_admin(current_user, staff_id)
    result = db.execute(
        text(
            """
            SELECT * FROM staff_service_overrides
            WHERE staff_id = :staff_id
            ORDER BY created_at DESC
            """
        ),
        {"staff_id": staff_id},
    )
    return [
        _normalize_uuid_fields(dict(row._mapping), ["id", "staff_id", "service_id"])
        for row in result.fetchall()
    ]

@router.delete("/overrides/{override_id}")
async def delete_staff_service_override(
    override_id: str,
    current_user: dict = Depends(require_permissions("staff:manage")),
    db: Session = Depends(get_db),
):
    """Delete a staff service override (Admin only)."""
    result = db.execute(
        text("DELETE FROM staff_service_overrides WHERE id = :id"),
        {"id": override_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "staff_service_override",
        override_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Override not found")

    return {"message": "Override deleted"}
