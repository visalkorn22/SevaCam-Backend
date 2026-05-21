from fastapi import APIRouter, Depends, HTTPException, status, UploadFile, File, Request
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import List
from pathlib import Path
from pydantic import BaseModel
from app.core.database import get_db
from app.core.auth import require_permissions
from app.core.config import settings
from app.core.image_moderation import moderate_image
from app.models.schemas import (
    ServiceCreate,
    ServiceUpdate,
    ServiceResponse,
    ServiceOperatingScheduleCreate,
    ServiceOperatingScheduleUpdate,
    ServiceOperatingRuleCreate,
    ServiceOperatingExceptionCreate,
)
import uuid


class ServiceLocationsUpdate(BaseModel):
    location_ids: List[str]

router = APIRouter()

_uploads_dir = Path(__file__).resolve().parent.parent / "uploads"
_uploads_dir.mkdir(parents=True, exist_ok=True)


def _normalize_service_row(row: dict) -> dict:
    if row.get("id") is not None:
        row["id"] = str(row["id"])
    if row.get("admin_id") is not None:
        row["admin_id"] = str(row["admin_id"])
    return row

def _normalize_uuid_fields(row: dict, fields: list[str]) -> dict:
    for field in fields:
        if row.get(field) is not None:
            row[field] = str(row[field])
    return row

@router.get("/", response_model=List[ServiceResponse])
async def get_services(
    active_only: bool = True,
    search: str | None = None,
    category: str | None = None,
    tag: str | None = None,
    min_price: float | None = None,
    max_price: float | None = None,
    min_duration: int | None = None,
    max_duration: int | None = None,
    require_staff: bool = False,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    """Get all services"""
    conditions = ["is_archived = FALSE"]
    params: dict[str, object] = {"limit": limit, "skip": skip}
    if active_only:
        conditions.append(
            "is_active = TRUE"
            " AND NOT (paused_from IS NOT NULL"
            " AND paused_from <= NOW()"
            " AND (paused_until IS NULL OR paused_until >= NOW()))"
        )
    if search:
        params["search"] = f"%{search}%"
        conditions.append(
            "(name ILIKE :search"
            " OR COALESCE(public_name, '') ILIKE :search"
            " OR COALESCE(description, '') ILIKE :search)"
        )
    if category:
        params["category"] = category
        conditions.append("category = :category")
    if tag:
        params["tag"] = tag
        conditions.append(":tag = ANY(tags)")
    if min_price is not None:
        params["min_price"] = min_price
        conditions.append("price >= :min_price")
    if max_price is not None:
        params["max_price"] = max_price
        conditions.append("price <= :max_price")
    if min_duration is not None:
        params["min_duration"] = min_duration
        conditions.append("duration_minutes >= :min_duration")
    if max_duration is not None:
        params["max_duration"] = max_duration
        conditions.append("duration_minutes <= :max_duration")
    if require_staff:
        conditions.append(
            "EXISTS ("
            "SELECT 1 FROM staff_services ss "
            "JOIN users u ON u.id = ss.staff_id "
            "WHERE ss.service_id = services.id "
            "AND u.is_active = TRUE "
            "AND ss.is_bookable = TRUE "
            "AND ss.is_temporarily_unavailable = FALSE "
            "AND ss.admin_only = FALSE)"
        )

    query = "SELECT * FROM services WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at DESC LIMIT :limit OFFSET :skip"

    result = db.execute(text(query), params)
    services = result.fetchall()
    return [_normalize_service_row(dict(row._mapping)) for row in services]

@router.get("/{service_id}", response_model=ServiceResponse)
async def get_service(service_id: str, db: Session = Depends(get_db)):
    """Get service by ID"""
    result = db.execute(
        text("SELECT * FROM services WHERE id = :id AND is_archived = FALSE"),
        {"id": service_id},
    )
    service = result.fetchone()
    
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    service_data = _normalize_service_row(dict(service._mapping))

    # Embed assigned locations
    loc_rows = db.execute(
        text("""
        SELECT l.id, l.name, l.address, l.latitude, l.longitude, l.timezone
        FROM locations l
        JOIN service_locations sl ON sl.location_id = l.id
        WHERE sl.service_id = :sid AND l.is_active = TRUE
        ORDER BY l.name
        """),
        {"sid": service_data["id"]},
    ).fetchall()
    service_data["locations"] = [
        {
            "id": str(r._mapping["id"]),
            "name": r._mapping["name"],
            "address": r._mapping["address"],
            "latitude": r._mapping["latitude"],
            "longitude": r._mapping["longitude"],
            "timezone": r._mapping["timezone"],
        }
        for r in loc_rows
    ]
    return service_data

@router.get("/{service_id}/locations")
def get_service_locations(service_id: str, db: Session = Depends(get_db)):
    """Return all locations assigned to a service."""
    rows = db.execute(
        text("""
        SELECT l.*
        FROM locations l
        JOIN service_locations sl ON sl.location_id = l.id
        WHERE sl.service_id = :service_id AND l.is_active = TRUE
        ORDER BY l.name
        """),
        {"service_id": service_id},
    ).fetchall()
    return [dict(row._mapping) for row in rows]


@router.put("/{service_id}/locations")
def set_service_locations(
    service_id: str,
    payload: ServiceLocationsUpdate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    """Replace the full set of locations for a service. payload: {"location_ids": [...]}"""
    location_ids: list[str] = payload.location_ids
    db.execute(
        text("DELETE FROM service_locations WHERE service_id = :sid"),
        {"sid": service_id},
    )
    for loc_id in location_ids:
        db.execute(
            text("INSERT INTO service_locations (service_id, location_id) VALUES (:sid, :lid)"),
            {"sid": service_id, "lid": loc_id},
        )
    db.commit()
    return {"ok": True, "location_ids": location_ids}


@router.post("/", response_model=ServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    service: ServiceCreate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db)
):
    """Create a new service (Admin only)"""
    service_id = str(uuid.uuid4())
    
    db.execute(
        text(
            """
            INSERT INTO services (
                id, admin_id, name, public_name, internal_name, category, tags,
                description, inclusions, prep_notes, duration_minutes,
                price, deposit_amount, buffer_minutes, max_capacity, is_active, image_url, image_urls,
                is_archived, paused_from, paused_until
            )
            VALUES (
                :id, :admin_id, :name, :public_name, :internal_name, :category, :tags,
                :description, :inclusions, :prep_notes, :duration_minutes,
                :price, :deposit_amount, :buffer_minutes, :max_capacity, :is_active, :image_url, :image_urls,
                :is_archived, :paused_from, :paused_until
            )
            """
        ),
        {
            "id": service_id,
            "admin_id": current_user.get("id"),
            "name": service.name,
            "public_name": service.public_name,
            "internal_name": service.internal_name,
            "category": service.category,
            "tags": service.tags,
            "description": service.description,
            "inclusions": service.inclusions,
            "prep_notes": service.prep_notes,
            "duration_minutes": service.duration_minutes,
            "price": service.price,
            "deposit_amount": service.deposit_amount,
            "buffer_minutes": service.buffer_minutes,
            "max_capacity": service.max_capacity,
            "is_active": service.is_active,
            "image_url": service.image_url,
            "image_urls": service.image_urls,
            "is_archived": False,
            "paused_from": None,
            "paused_until": None,
        },
    )
    db.commit()
    
    return await get_service(service_id, db)

@router.post("/upload-image")
async def upload_service_image(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(require_permissions("services:manage")),
):
    """Upload a service image and return its URL (Admin only)"""
    if not file.content_type or not file.content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="Only image uploads are allowed")

    extension = Path(file.filename or "").suffix.lower()
    if extension == "":
        extension = ".jpg"

    filename = f"{uuid.uuid4()}{extension}"
    destination = _uploads_dir / filename

    contents = await file.read()

    allowed, reason = moderate_image(
        content=contents,
        filename=file.filename or "upload",
        content_type=file.content_type or "application/octet-stream",
    )
    if not allowed:
        message = "Image rejected by safety policy"
        if reason:
            message = f"{message}: {reason}"
        raise HTTPException(status_code=400, detail=message)
    destination.write_bytes(contents)

    base_url = str(request.base_url).rstrip("/")
    return {"image_url": f"{base_url}/uploads/{filename}"}

@router.put("/{service_id}", response_model=ServiceResponse)
async def update_service(
    service_id: str,
    service: ServiceUpdate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db)
):
    """Update a service (Admin only)"""
    # Build update query dynamically
    updates = []
    params = {"id": service_id}
    
    if service.name is not None:
        updates.append("name = :name")
        params["name"] = service.name
    if service.public_name is not None:
        updates.append("public_name = :public_name")
        params["public_name"] = service.public_name
    if service.internal_name is not None:
        updates.append("internal_name = :internal_name")
        params["internal_name"] = service.internal_name
    if service.category is not None:
        updates.append("category = :category")
        params["category"] = service.category
    if service.tags is not None:
        updates.append("tags = :tags")
        params["tags"] = service.tags
    if service.description is not None:
        updates.append("description = :description")
        params["description"] = service.description
    if service.inclusions is not None:
        updates.append("inclusions = :inclusions")
        params["inclusions"] = service.inclusions
    if service.prep_notes is not None:
        updates.append("prep_notes = :prep_notes")
        params["prep_notes"] = service.prep_notes
    if service.duration_minutes is not None:
        updates.append("duration_minutes = :duration_minutes")
        params["duration_minutes"] = service.duration_minutes
    if service.price is not None:
        updates.append("price = :price")
        params["price"] = service.price
    if service.deposit_amount is not None:
        updates.append("deposit_amount = :deposit_amount")
        params["deposit_amount"] = service.deposit_amount
    if service.buffer_minutes is not None:
        updates.append("buffer_minutes = :buffer_minutes")
        params["buffer_minutes"] = service.buffer_minutes
    if service.max_capacity is not None:
        updates.append("max_capacity = :max_capacity")
        params["max_capacity"] = service.max_capacity
    if service.is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = service.is_active
    if service.image_url is not None:
        updates.append("image_url = :image_url")
        params["image_url"] = service.image_url
    if service.image_urls is not None:
        updates.append("image_urls = :image_urls")
        params["image_urls"] = service.image_urls
    if service.paused_from is not None:
        updates.append("paused_from = :paused_from")
        params["paused_from"] = service.paused_from
    if service.paused_until is not None:
        updates.append("paused_until = :paused_until")
        params["paused_until"] = service.paused_until
    
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    
    query = f"UPDATE services SET {', '.join(updates)} WHERE id = :id"
    db.execute(text(query), params)
    db.commit()
    
    return await get_service(service_id, db)

@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: str,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    """Archive a service (Admin only)"""
    if settings.FEATURE_SET == "full":
        upcoming = db.execute(
            text(
                """
                SELECT COUNT(1)
                FROM bookings
                WHERE service_id = :id
                  AND start_time_utc > NOW()
                  AND status IN ('pending', 'confirmed')
                """
            ),
            {"id": service_id},
        ).scalar()

        if upcoming and upcoming > 0:
            raise HTTPException(
                status_code=400,
                detail="Cannot archive service with future bookings",
            )

    result = db.execute(
        text(
            """
            UPDATE services
            SET is_archived = TRUE,
                archived_at = NOW(),
                is_active = FALSE
            WHERE id = :id AND is_archived = FALSE
            """
        ),
        {"id": service_id},
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Service not found")

    return None

@router.get("/{service_id}/operating-schedule")
async def get_service_operating_schedule(
    service_id: str,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    schedule = db.execute(
        text(
            """
            SELECT * FROM service_operating_schedules
            WHERE service_id = :service_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    ).fetchone()

    if not schedule:
        return {"schedule": None, "rules": [], "exceptions": []}

    schedule_dict = _normalize_uuid_fields(
        dict(schedule._mapping),
        ["id", "service_id"],
    )
    schedule_id = schedule_dict["id"]

    rules = db.execute(
        text(
            """
            SELECT * FROM service_operating_rules
            WHERE schedule_id = :schedule_id
            ORDER BY created_at DESC
            """
        ),
        {"schedule_id": schedule_id},
    ).fetchall()

    exceptions = db.execute(
        text(
            """
            SELECT * FROM service_operating_exceptions
            WHERE service_id = :service_id
            ORDER BY date DESC
            """
        ),
        {"service_id": service_id},
    ).fetchall()

    rules_data = [
        _normalize_uuid_fields(dict(row._mapping), ["id", "schedule_id"])
        for row in rules
    ]
    exceptions_data = [
        _normalize_uuid_fields(dict(row._mapping), ["id", "service_id"])
        for row in exceptions
    ]

    return {
        "schedule": schedule_dict,
        "rules": rules_data,
        "exceptions": exceptions_data,
    }

@router.post("/{service_id}/operating-schedule")
async def create_service_operating_schedule(
    service_id: str,
    payload: ServiceOperatingScheduleCreate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    schedule_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO service_operating_schedules
                (id, service_id, timezone, rule_type, open_time, close_time,
                 effective_from, effective_to, is_active)
            VALUES
                (:id, :service_id, :timezone, :rule_type, :open_time, :close_time,
                 :effective_from, :effective_to, :is_active)
            """
        ),
        {
            "id": schedule_id,
            "service_id": service_id,
            "timezone": payload.timezone,
            "rule_type": payload.rule_type,
            "open_time": payload.open_time,
            "close_time": payload.close_time,
            "effective_from": payload.effective_from,
            "effective_to": payload.effective_to,
            "is_active": payload.is_active,
        },
    )
    db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.put("/{service_id}/operating-schedule")
async def update_service_operating_schedule(
    service_id: str,
    payload: ServiceOperatingScheduleUpdate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    schedule = db.execute(
        text(
            """
            SELECT id FROM service_operating_schedules
            WHERE service_id = :service_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    ).fetchone()

    if not schedule:
        return await create_service_operating_schedule(service_id, payload, current_user, db)

    updates = []
    params: dict[str, object] = {"id": schedule[0]}

    if payload.timezone is not None:
        updates.append("timezone = :timezone")
        params["timezone"] = payload.timezone
    if payload.rule_type is not None:
        updates.append("rule_type = :rule_type")
        params["rule_type"] = payload.rule_type
    if payload.open_time is not None:
        updates.append("open_time = :open_time")
        params["open_time"] = payload.open_time
    if payload.close_time is not None:
        updates.append("close_time = :close_time")
        params["close_time"] = payload.close_time
    if payload.effective_from is not None:
        updates.append("effective_from = :effective_from")
        params["effective_from"] = payload.effective_from
    if payload.effective_to is not None:
        updates.append("effective_to = :effective_to")
        params["effective_to"] = payload.effective_to
    if payload.is_active is not None:
        updates.append("is_active = :is_active")
        params["is_active"] = payload.is_active

    if updates:
        query = f"UPDATE service_operating_schedules SET {', '.join(updates)} WHERE id = :id"
        db.execute(text(query), params)
        db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.post("/{service_id}/operating-schedule/rules")
async def add_service_operating_rule(
    service_id: str,
    payload: ServiceOperatingRuleCreate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    schedule = db.execute(
        text(
            """
            SELECT id FROM service_operating_schedules
            WHERE service_id = :service_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id},
    ).fetchone()

    if not schedule:
        raise HTTPException(status_code=400, detail="Service schedule not configured")

    rule_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO service_operating_rules
                (id, schedule_id, rule_type, weekday, month_day, nth, start_time, end_time)
            VALUES
                (:id, :schedule_id, :rule_type, :weekday, :month_day, :nth, :start_time, :end_time)
            """
        ),
        {
            "id": rule_id,
            "schedule_id": schedule[0],
            "rule_type": payload.rule_type,
            "weekday": payload.weekday,
            "month_day": payload.month_day,
            "nth": payload.nth,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
        },
    )
    db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.delete("/{service_id}/operating-schedule/rules/{rule_id}")
async def delete_service_operating_rule(
    service_id: str,
    rule_id: str,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    db.execute(
        text("DELETE FROM service_operating_rules WHERE id = :id"),
        {"id": rule_id},
    )
    db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.post("/{service_id}/operating-schedule/exceptions")
async def add_service_operating_exception(
    service_id: str,
    payload: ServiceOperatingExceptionCreate,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    exception_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO service_operating_exceptions
                (id, service_id, date, is_open, start_time, end_time, reason)
            VALUES
                (:id, :service_id, :date, :is_open, :start_time, :end_time, :reason)
            """
        ),
        {
            "id": exception_id,
            "service_id": service_id,
            "date": payload.date,
            "is_open": payload.is_open,
            "start_time": payload.start_time,
            "end_time": payload.end_time,
            "reason": payload.reason,
        },
    )
    db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.delete("/{service_id}/operating-schedule/exceptions/{exception_id}")
async def delete_service_operating_exception(
    service_id: str,
    exception_id: str,
    current_user: dict = Depends(require_permissions("services:manage")),
    db: Session = Depends(get_db),
):
    db.execute(
        text("DELETE FROM service_operating_exceptions WHERE id = :id"),
        {"id": exception_id},
    )
    db.commit()

    return await get_service_operating_schedule(service_id, current_user, db)

@router.get("/{service_id}/staff")
async def get_service_staff(service_id: str, db: Session = Depends(get_db)):
    """Get staff assigned to a service"""
    result = db.execute(
        text(
            """
            SELECT u.id, u.full_name, u.avatar_url,
                   ss.price_override, ss.deposit_override, ss.duration_override,
                   ss.buffer_override, ss.capacity_override
            FROM staff_services ss
            JOIN users u ON u.id = ss.staff_id
            WHERE ss.service_id = :service_id AND u.is_active = TRUE
              AND u.role = 'staff'
              AND ss.is_bookable = TRUE
              AND ss.is_temporarily_unavailable = FALSE
              AND ss.admin_only = FALSE
            ORDER BY u.full_name
            """
        ),
        {"service_id": service_id},
    )

    return [
        {
            "id": row[0],
            "name": row[1] or "Staff Member",
            "avatar_url": row[2],
            "price_override": row[3],
            "deposit_override": row[4],
            "duration_override": row[5],
            "buffer_override": row[6],
            "capacity_override": row[7],
        }
        for row in result.fetchall()
    ]


def _format_customer_name(full_name: str | None) -> str:
    """Return 'First L.' format for privacy, e.g. 'Jane D.'"""
    if not full_name:
        return "Customer"
    parts = full_name.strip().split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0]}."


@router.get("/{service_id}/reviews")
async def get_service_reviews(service_id: str, db: Session = Depends(get_db)):
    """Public endpoint: approved review summary for a service."""
    agg_row = db.execute(
        text(
            """
            SELECT COUNT(*) AS review_count, AVG(r.rating) AS average_rating
            FROM reviews r
            JOIN bookings b ON r.booking_id = b.id
            WHERE b.service_id = :service_id AND r.is_approved = true
            """
        ),
        {"service_id": service_id},
    ).fetchone()

    review_count = int(agg_row[0]) if agg_row else 0
    raw_avg = agg_row[1] if agg_row else None
    average_rating = round(float(raw_avg), 1) if raw_avg is not None else None

    rows = db.execute(
        text(
            """
            SELECT r.rating, r.comment, r.created_at, c.full_name
            FROM reviews r
            JOIN bookings b ON r.booking_id = b.id
            JOIN customers c ON b.customer_id = c.id
            WHERE b.service_id = :service_id AND r.is_approved = true
            ORDER BY r.created_at DESC
            LIMIT 10
            """
        ),
        {"service_id": service_id},
    ).fetchall()

    reviews = [
        {
            "rating": row[0],
            "comment": row[1],
            "created_at": (
                row[2].isoformat() if hasattr(row[2], "isoformat") else str(row[2])
            ),
            "customer_name": _format_customer_name(row[3]),
        }
        for row in rows
    ]

    return {
        "average_rating": average_rating,
        "review_count": review_count,
        "reviews": reviews,
    }
