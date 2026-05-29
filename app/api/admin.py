import csv
import io

from fastapi import APIRouter, Depends, HTTPException
from typing import List
from sqlalchemy import text
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.core.auth import require_roles
from app.core.audit import log_audit
from app.core.database import get_db
from app.core.config import settings
from app.core.staff_profiles import calculate_experience_level, round_average_rating
from app.models.schemas import LocationCreate, LocationUpdate, LocationResponse
import uuid

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _csv_response(rows: list[list[str]], filename: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


def _serialize_location(row) -> dict:
    data = dict(row._mapping)
    data["id"] = str(data["id"])
    return data


@router.get("/staff")
def list_staff(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    stats_select = "NULL AS average_rating, 0 AS review_count, 0 AS completed_bookings"
    stats_join = ""
    if settings.FEATURE_SET == "full":
        stats_select = """
            stats.average_rating AS average_rating,
            COALESCE(stats.review_count, 0) AS review_count,
            COALESCE(stats.completed_bookings, 0) AS completed_bookings
        """
        stats_join = """
            LEFT JOIN (
                SELECT
                    b.staff_id,
                    COUNT(CASE WHEN b.status = 'completed' THEN 1 END) AS completed_bookings,
                    AVG(CASE WHEN b.status = 'completed' AND r.is_approved = TRUE THEN r.rating END) AS average_rating,
                    COUNT(CASE WHEN b.status = 'completed' AND r.is_approved = TRUE THEN 1 END) AS review_count
                FROM bookings b
                LEFT JOIN reviews r ON r.booking_id = b.id
                GROUP BY b.staff_id
            ) stats ON stats.staff_id = u.id
        """

    result = db.execute(
        text(
            f"""
            SELECT
                u.id, u.full_name, u.avatar_url, u.phone, u.email, u.role, u.is_active,
                {stats_select}
            FROM users u
            {stats_join}
            WHERE u.role IN ('staff', 'admin', 'superadmin')
            ORDER BY u.full_name
            """
        )
    )
    staff_rows = result.fetchall()

    return [
        {
            "id": row[0],
            "full_name": row[1],
            "avatar_url": row[2],
            "phone": row[3],
            "email": row[4],
            "role": "admin" if row[5] == "superadmin" else row[5],
            "is_active": bool(row[6]),
            "average_rating": round_average_rating(row[7]),
            "review_count": int(row[8] or 0),
            "completed_bookings": int(row[9] or 0),
            "experience_level": calculate_experience_level(row[7], row[9]),
        }
        for row in staff_rows
    ]

@router.get("/locations", response_model=List[LocationResponse])
def list_locations(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    result = db.execute(
        text("SELECT * FROM locations ORDER BY created_at DESC")
    )
    return [_serialize_location(row) for row in result.fetchall()]

@router.post("/locations", response_model=LocationResponse)
def create_location(
    payload: LocationCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    location_id = str(uuid.uuid4())
    db.execute(
        text("""
        INSERT INTO locations (id, name, timezone, address, latitude, longitude, is_active)
        VALUES (:id, :name, :timezone, :address, :latitude, :longitude, :is_active)
        """),
        {
            "id": location_id,
            "name": payload.name,
            "timezone": payload.timezone,
            "address": payload.address,
            "latitude": payload.latitude,
            "longitude": payload.longitude,
            "is_active": payload.is_active,
        },
    )
    log_audit(
        db,
        current_user.get("id"),
        "create",
        "location",
        location_id,
        payload.model_dump(),
    )
    db.commit()
    created = db.execute(
        text("SELECT * FROM locations WHERE id = :id"), {"id": location_id}
    ).fetchone()
    return _serialize_location(created)

@router.put("/locations/{location_id}", response_model=LocationResponse)
def update_location(
    location_id: str,
    payload: LocationUpdate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    updates = []
    params = {"id": location_id}

    if "name" in payload.model_fields_set:
        updates.append("name = :name")
        params["name"] = payload.name
    if "timezone" in payload.model_fields_set:
        updates.append("timezone = :timezone")
        params["timezone"] = payload.timezone
    if "address" in payload.model_fields_set:
        updates.append("address = :address")
        params["address"] = payload.address
    if "is_active" in payload.model_fields_set:
        updates.append("is_active = :is_active")
        params["is_active"] = payload.is_active
    if "latitude" in payload.model_fields_set:
        updates.append("latitude = :latitude")
        params["latitude"] = payload.latitude
    if "longitude" in payload.model_fields_set:
        updates.append("longitude = :longitude")
        params["longitude"] = payload.longitude

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    result = db.execute(
        text(f"UPDATE locations SET {', '.join(updates)} WHERE id = :id"),
        params,
    )

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Location not found")

    log_audit(
        db,
        current_user.get("id"),
        "update",
        "location",
        location_id,
        payload.model_dump(),
    )
    db.commit()

    updated = db.execute(
        text("SELECT * FROM locations WHERE id = :id"), {"id": location_id}
    ).fetchone()
    return _serialize_location(updated)

@router.delete("/locations/{location_id}")
def delete_location(
    location_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    result = db.execute(
        text("DELETE FROM locations WHERE id = :id"),
        {"id": location_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "location",
        location_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Location not found")

    return {"message": "Location deleted"}


@router.get("/services")
def list_services(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    result = db.execute(
        text(
            """
            SELECT id, name, description, image_url, image_urls, is_active, duration_minutes, price, deposit_amount,
                   buffer_minutes, max_capacity, is_archived, archived_at, paused_from, paused_until
            FROM services
            WHERE is_archived = FALSE
            ORDER BY created_at DESC
            """
        )
    )
    services = []
    for row in result.fetchall():
        data = dict(row._mapping)
        if data.get("id") is not None:
            data["id"] = str(data["id"])
        if data.get("admin_id") is not None:
            data["admin_id"] = str(data["admin_id"])
        services.append(data)
    return services


@router.get("/bookings")
def list_bookings(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT b.id, b.start_time_utc, b.status, b.payment_status,
               s.id as service_id, s.name as service_name, s.price, s.duration_minutes,
               u.id as staff_id, u.full_name as staff_name,
               c.id as customer_id, c.full_name as customer_name, c.email as customer_email, c.phone as customer_phone
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        LEFT JOIN customers c ON b.customer_id = c.id
        ORDER BY b.start_time_utc DESC
        """
    )
    rows = result.fetchall()

    return [
        {
            "id": row[0],
            "start_time_utc": row[1],
            "status": row[2],
            "payment_status": row[3],
            "service": {
                "id": row[4],
                "name": row[5],
                "price": row[6],
                "duration_minutes": row[7],
            },
            "staff": {"id": row[8], "full_name": row[9]},
            "customer": {
                "id": row[10],
                "full_name": row[11],
                "email": row[12],
                "phone": row[13],
            },
        }
        for row in rows
    ]


@router.get("/payments")
def list_payments(
    limit: int = 50,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT p.id, p.created_at, p.amount, p.status, p.provider,
               b.id as booking_id, s.name as service_name, c.full_name as customer_name
        FROM payments p
        LEFT JOIN bookings b ON p.booking_id = b.id
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN customers c ON b.customer_id = c.id
        ORDER BY p.created_at DESC
        LIMIT :limit
        """,
        {"limit": limit},
    )
    rows = result.fetchall()

    return [
        {
            "id": row[0],
            "created_at": row[1],
            "amount": row[2],
            "status": row[3],
            "payment_method": row[4],
            "booking": {
                "id": row[5],
                "service": {"name": row[6]},
                "customer": {"full_name": row[7]},
            },
        }
        for row in rows
    ]


@router.get("/reviews")
def list_reviews(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT r.id, r.rating, r.comment, r.is_approved, r.created_at,
               c.full_name as customer_name, s.name as service_name
        FROM reviews r
        LEFT JOIN bookings b ON r.booking_id = b.id
        LEFT JOIN customers c ON b.customer_id = c.id
        LEFT JOIN services s ON b.service_id = s.id
        ORDER BY r.created_at DESC
        """
    )
    rows = result.fetchall()

    return [
        {
            "id": row[0],
            "rating": row[1],
            "comment": row[2],
            "is_visible": bool(row[3]),
            "created_at": row[4],
            "customer": {"full_name": row[5]},
            "service": {"name": row[6]},
        }
        for row in rows
    ]


@router.get("/audit-logs")
def list_audit_logs(
    limit: int = 50,
    offset: int = 0,
    entity_type: str | None = None,
    action: str | None = None,
    current_user: dict = Depends(require_roles("superadmin")),
    db: Session = Depends(get_db),
):
    filters = []
    params: dict = {"limit": limit, "offset": offset}

    if entity_type:
        filters.append("a.entity_type = :entity_type")
        params["entity_type"] = entity_type
    if action:
        filters.append("a.action = :action")
        params["action"] = action

    where = ("WHERE " + " AND ".join(filters)) if filters else ""

    rows = db.execute(
        text(f"""
            SELECT a.id, a.actor_id, a.action, a.entity_type, a.entity_id,
                   a.changes, a.created_at,
                   u.full_name AS actor_name, u.email AS actor_email
            FROM audit_logs a
            LEFT JOIN users u ON a.actor_id::uuid = u.id
            {where}
            ORDER BY a.created_at DESC
            LIMIT :limit OFFSET :offset
        """),
        params,
    ).fetchall()

    total = db.execute(
        text(f"SELECT COUNT(*) FROM audit_logs a {where}"),
        {k: v for k, v in params.items() if k not in ("limit", "offset")},
    ).scalar()

    return {
        "total": total,
        "items": [
            {
                "id": str(row.id),
                "actor_id": str(row.actor_id) if row.actor_id else None,
                "actor_name": row.actor_name,
                "actor_email": row.actor_email,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": str(row.entity_id) if row.entity_id else None,
                "changes": row.changes,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }


@router.get("/reports/bookings.csv")
def report_bookings(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT b.id, b.start_time_utc, b.status, b.payment_status,
               s.name as service_name,
               u.full_name as staff_name,
               c.full_name as customer_name
        FROM bookings b
        LEFT JOIN services s ON b.service_id = s.id
        LEFT JOIN users u ON b.staff_id = u.id
        LEFT JOIN customers c ON b.customer_id = c.id
        ORDER BY b.start_time_utc DESC
        """
    )
    rows = [["booking_id", "start_time_utc", "status", "payment_status", "service", "staff", "customer"]]
    rows.extend([list(map(str, row)) for row in result.fetchall()])
    return _csv_response(rows, "bookings.csv")


@router.get("/reports/financial.csv")
def report_financial(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT p.id, p.booking_id, p.amount, p.currency, p.status, p.provider, p.created_at
        FROM payments p
        ORDER BY p.created_at DESC
        """
    )
    rows = [["payment_id", "booking_id", "amount", "currency", "status", "provider", "created_at"]]
    rows.extend([list(map(str, row)) for row in result.fetchall()])
    return _csv_response(rows, "financial.csv")


@router.get("/reports/customers.csv")
def report_customers(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    if settings.FEATURE_SET != "full":
        raise HTTPException(status_code=404, detail="Not available in core mode")
    result = db.execute(
        """
        SELECT id, full_name, email, phone, timezone, is_blocked, created_at
        FROM customers
        ORDER BY created_at DESC
        """
    )
    rows = [["customer_id", "full_name", "email", "phone", "timezone", "is_blocked", "created_at"]]
    rows.extend([list(map(str, row)) for row in result.fetchall()])
    return _csv_response(rows, "customers.csv")


@router.get("/reports/staff.csv")
def report_staff(
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    result = db.execute(
        """
        SELECT id, full_name, email, role, phone, is_active, created_at
        FROM users
        WHERE role IN ('staff', 'admin', 'superadmin')
        ORDER BY full_name
        """
    )
    rows = [["staff_id", "full_name", "email", "role", "phone", "is_active", "created_at"]]
    rows.extend([list(map(str, row)) for row in result.fetchall()])
    return _csv_response(rows, "staff.csv")
