from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from sqlalchemy.orm import Session
import uuid

from app.core.auth import get_current_user, is_admin
from app.core.database import get_db
from app.models.schemas import CustomerCreate, CustomerResponse

router = APIRouter(prefix="/api/customers", tags=["customers"])
DEFAULT_APP_TIMEZONE = "Asia/Phnom_Penh"


@router.post("", response_model=CustomerResponse)
def create_customer(
    payload: CustomerCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    role = current_user.get("role")
    if role == "staff":
        raise HTTPException(status_code=403, detail="Forbidden")

    if role == "customer":
        if payload.user_id and payload.user_id != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")
        payload.user_id = current_user.get("id")

    existing = None
    if payload.user_id:
        existing = db.execute(
            "SELECT * FROM customers WHERE user_id = :user_id",
            {"user_id": payload.user_id},
        ).fetchone()
    if not existing:
        existing = db.execute(
            "SELECT * FROM customers WHERE email = :email",
            {"email": payload.email},
        ).fetchone()

    if existing:
        existing_map = dict(existing._mapping)
        next_full_name = payload.full_name or existing_map.get("full_name")
        next_phone = payload.phone if payload.phone is not None else existing_map.get("phone")
        next_timezone = payload.timezone or existing_map.get("timezone") or DEFAULT_APP_TIMEZONE

        if (
            next_full_name != existing_map.get("full_name")
            or next_phone != existing_map.get("phone")
            or next_timezone != existing_map.get("timezone")
        ):
            updated = db.execute(
                """
                UPDATE customers
                SET full_name = :full_name,
                    phone = :phone,
                    timezone = :timezone
                WHERE id = :id
                RETURNING id, user_id, full_name, email, phone, timezone, notes, is_blocked, created_at
                """,
                {
                    "id": existing_map["id"],
                    "full_name": next_full_name,
                    "phone": next_phone,
                    "timezone": next_timezone,
                },
            ).fetchone()
            db.commit()
            return jsonable_encoder(dict(updated._mapping))

        return jsonable_encoder(existing_map)

    customer_id = str(uuid.uuid4())
    created = db.execute(
        """
        INSERT INTO customers (id, user_id, full_name, email, phone, timezone, notes)
        VALUES (:id, :user_id, :full_name, :email, :phone, :timezone, :notes)
        RETURNING id, user_id, full_name, email, phone, timezone, notes, is_blocked, created_at
        """,
        {
            "id": customer_id,
            "user_id": payload.user_id,
            "full_name": payload.full_name,
            "email": payload.email,
            "phone": payload.phone,
            "timezone": payload.timezone or DEFAULT_APP_TIMEZONE,
            "notes": payload.notes,
        },
    ).fetchone()
    db.commit()
    return jsonable_encoder(dict(created._mapping))


@router.get("/{customer_id}", response_model=CustomerResponse)
def get_customer(
    customer_id: str,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    record = db.execute(
        "SELECT * FROM customers WHERE id = :id",
        {"id": customer_id},
    ).fetchone()

    if not record:
        raise HTTPException(status_code=404, detail="Customer not found")

    if not is_admin(current_user):
        if record.user_id != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")

    return jsonable_encoder(dict(record._mapping))
