from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from app.core.database import get_db
from app.core.auth import get_current_user
from app.models.schemas import ReviewCreate, ReviewResponse
import uuid

router = APIRouter()


@router.post("/", response_model=ReviewResponse)
async def create_review(
    review: ReviewCreate,
    current_user: dict = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Submit a review for a completed booking. Customers only; one review per booking."""
    if current_user.get("role") != "customer":
        raise HTTPException(status_code=403, detail="Only customers can submit reviews")

    # Resolve the customer record from the authenticated user
    customer_row = db.execute(
        text("SELECT id FROM customers WHERE user_id = :uid"),
        {"uid": current_user["id"]},
    ).fetchone()
    if not customer_row:
        raise HTTPException(status_code=403, detail="Customer profile not found")
    customer_id = str(dict(customer_row._mapping)["id"])

    # Fetch the booking
    booking_row = db.execute(
        text("SELECT id, customer_id, status FROM bookings WHERE id = :id"),
        {"id": review.booking_id},
    ).fetchone()
    if not booking_row:
        raise HTTPException(status_code=404, detail="Booking not found")
    booking = dict(booking_row._mapping)

    # Ownership check
    if str(booking["customer_id"]) != customer_id:
        raise HTTPException(status_code=403, detail="You can only review your own bookings")

    # Status check
    if booking["status"] != "completed":
        raise HTTPException(status_code=400, detail="Only completed bookings can be reviewed")

    # Duplicate check
    existing = db.execute(
        text("SELECT id FROM reviews WHERE booking_id = :bid"),
        {"bid": review.booking_id},
    ).fetchone()
    if existing:
        raise HTTPException(status_code=400, detail="This booking has already been reviewed")

    # Insert — auto-approved (no moderation flow yet)
    review_id = str(uuid.uuid4())
    db.execute(
        text(
            """
            INSERT INTO reviews (id, booking_id, rating, comment, is_approved, created_at)
            VALUES (:id, :booking_id, :rating, :comment, true, NOW())
            """
        ),
        {
            "id": review_id,
            "booking_id": review.booking_id,
            "rating": review.rating,
            "comment": review.comment,
        },
    )
    db.commit()

    # Fetch and return the created review
    row = db.execute(
        text(
            "SELECT id, booking_id, rating, comment, is_approved, created_at "
            "FROM reviews WHERE id = :id"
        ),
        {"id": review_id},
    ).fetchone()
    return dict(row._mapping)
