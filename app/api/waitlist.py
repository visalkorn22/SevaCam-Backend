from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import List, Optional
from app.core.database import get_db
from app.core.auth import get_current_user, is_admin, is_staff
from app.models.schemas import WaitlistCreate, WaitlistUpdate, WaitlistResponse
import uuid

router = APIRouter()

ALLOWED_WAITLIST_STATUSES = {"active", "notified", "booked", "expired"}


def _get_customer_id(db: Session, user_id: str) -> str | None:
    record = db.execute(
        "SELECT id FROM customers WHERE user_id = :user_id",
        {"user_id": user_id},
    ).fetchone()
    return record[0] if record else None


def _normalize_waitlist_row(row) -> dict:
    data = dict(row._mapping)
    if data.get("id") is not None:
        data["id"] = str(data["id"])
    if data.get("service_id") is not None:
        data["service_id"] = str(data["service_id"])
    if data.get("customer_id") is not None:
        data["customer_id"] = str(data["customer_id"])
    return data


def _ensure_waitlist_access(db: Session, waitlist_id: str, current_user: dict) -> dict:
    row = db.execute(
        "SELECT id, customer_id FROM waitlist WHERE id = :id",
        {"id": waitlist_id},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")

    if is_admin(current_user) or is_staff(current_user):
        return {"id": row[0], "customer_id": row[1]}

    if current_user.get("role") == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id or row[1] != customer_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        return {"id": row[0], "customer_id": row[1]}

    raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/", response_model=WaitlistResponse)
async def create_waitlist_entry(
    payload: WaitlistCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a waitlist entry for a service."""
    if current_user.get("role") == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id:
            raise HTTPException(status_code=403, detail="Customer profile not found")
        if payload.customer_id and payload.customer_id != customer_id:
            raise HTTPException(status_code=403, detail="Forbidden")
        payload_customer_id = customer_id
    elif is_staff(current_user) or is_admin(current_user):
        if not payload.customer_id:
            raise HTTPException(status_code=400, detail="customer_id is required")
        payload_customer_id = payload.customer_id
    else:
        raise HTTPException(status_code=403, detail="Forbidden")

    service_exists = db.execute(
        "SELECT 1 FROM services WHERE id = :id AND is_archived = FALSE",
        {"id": payload.service_id},
    ).fetchone()
    if not service_exists:
        raise HTTPException(status_code=404, detail="Service not found")

    entry_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO waitlist (id, service_id, customer_id, preferred_date, status)
        VALUES (:id, :service_id, :customer_id, :preferred_date, 'active')
        """,
        {
            "id": entry_id,
            "service_id": payload.service_id,
            "customer_id": payload_customer_id,
            "preferred_date": payload.preferred_date,
        },
    )
    db.commit()

    created = db.execute(
        "SELECT * FROM waitlist WHERE id = :id",
        {"id": entry_id},
    ).fetchone()
    return _normalize_waitlist_row(created)


@router.get("/", response_model=List[WaitlistResponse])
async def list_waitlist_entries(
    service_id: Optional[str] = None,
    customer_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List waitlist entries (customers see their own)."""
    query = "SELECT * FROM waitlist WHERE 1=1"
    params: dict[str, object] = {}

    if current_user.get("role") == "customer":
        customer_id = _get_customer_id(db, current_user.get("id"))
        if not customer_id:
            raise HTTPException(status_code=403, detail="Customer profile not found")
    elif not (is_staff(current_user) or is_admin(current_user)):
        raise HTTPException(status_code=403, detail="Forbidden")

    if service_id:
        query += " AND service_id = :service_id"
        params["service_id"] = service_id
    if customer_id:
        query += " AND customer_id = :customer_id"
        params["customer_id"] = customer_id
    if status:
        query += " AND status = :status"
        params["status"] = status

    query += " ORDER BY created_at DESC"
    rows = db.execute(query, params).fetchall()
    return [_normalize_waitlist_row(row) for row in rows]


@router.get("/{waitlist_id}", response_model=WaitlistResponse)
async def get_waitlist_entry(
    waitlist_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get a single waitlist entry."""
    _ensure_waitlist_access(db, waitlist_id, current_user)
    row = db.execute(
        "SELECT * FROM waitlist WHERE id = :id",
        {"id": waitlist_id},
    ).fetchone()
    return _normalize_waitlist_row(row)


@router.put("/{waitlist_id}", response_model=WaitlistResponse)
async def update_waitlist_entry(
    waitlist_id: str,
    payload: WaitlistUpdate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Update waitlist entry (staff/admin can update status)."""
    entry = _ensure_waitlist_access(db, waitlist_id, current_user)

    updates = []
    params: dict[str, object] = {"id": waitlist_id}

    if payload.preferred_date is not None:
        updates.append("preferred_date = :preferred_date")
        params["preferred_date"] = payload.preferred_date

    if payload.status is not None:
        if current_user.get("role") == "customer":
            raise HTTPException(status_code=403, detail="Forbidden")
        if payload.status not in ALLOWED_WAITLIST_STATUSES:
            raise HTTPException(status_code=400, detail="Invalid status")
        updates.append("status = :status")
        params["status"] = payload.status

    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")

    db.execute(
        f"UPDATE waitlist SET {', '.join(updates)} WHERE id = :id",
        params,
    )
    db.commit()

    row = db.execute(
        "SELECT * FROM waitlist WHERE id = :id",
        {"id": entry["id"]},
    ).fetchone()
    return _normalize_waitlist_row(row)


@router.delete("/{waitlist_id}")
async def delete_waitlist_entry(
    waitlist_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete a waitlist entry."""
    _ensure_waitlist_access(db, waitlist_id, current_user)

    result = db.execute(
        "DELETE FROM waitlist WHERE id = :id",
        {"id": waitlist_id},
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Waitlist entry not found")

    return {"message": "Waitlist entry deleted"}
