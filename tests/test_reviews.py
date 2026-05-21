"""
Unit tests for POST /api/reviews.
All DB calls are mocked — no real database needed.
"""
import os
import sys
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from app.core.database import get_db
from app.core.auth import get_current_user

# ── Fake row helper ──────────────────────────────────────────────────────────

class FakeRow:
    """Minimal stand-in for a SQLAlchemy Row."""
    def __init__(self, **kwargs):
        self._mapping = kwargs
    def __bool__(self):
        return True


def make_db(*fetchone_sequence):
    """
    Returns a mock db whose successive execute().fetchone() calls yield
    each item in fetchone_sequence in order.
    None means the query returned no row.
    """
    mock_db = MagicMock()
    calls = iter(fetchone_sequence)

    def _execute(*args, **kwargs):
        result = MagicMock()
        result.fetchone.return_value = next(calls, None)
        return result

    mock_db.execute.side_effect = _execute
    return mock_db


# ── Test app factory ─────────────────────────────────────────────────────────

def make_test_app(mock_db, mock_user):
    from app.api.reviews import router
    app = FastAPI()
    app.include_router(router, prefix="/api/reviews")
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user
    return app


CUSTOMER_USER = {
    "id": "user-1",
    "role": "customer",
    "email": "test@example.com",
    "is_active": True,
    "email_verified": True,
}

VALID_PAYLOAD = {"booking_id": "booking-1", "rating": 5, "comment": "Great!"}

CUSTOMER_ROW    = FakeRow(id="customer-1")
BOOKING_ROW     = FakeRow(id="booking-1", customer_id="customer-1", status="completed")
NO_REVIEW_ROW   = None
INSERTED_REVIEW = FakeRow(
    id="review-1",
    booking_id="booking-1",
    rating=5,
    comment="Great!",
    is_approved=True,
    created_at="2026-03-30T10:00:00",
)

# ── Tests ────────────────────────────────────────────────────────────────────

def test_returns_403_when_no_customer_profile():
    db = make_db(None)  # customers query returns nothing
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 403
    assert "Customer profile not found" in res.json()["detail"]


def test_returns_404_when_booking_not_found():
    db = make_db(CUSTOMER_ROW, None)  # customer found, booking not found
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 404
    assert "Booking not found" in res.json()["detail"]


def test_returns_403_when_booking_belongs_to_different_customer():
    other_booking = FakeRow(id="booking-1", customer_id="other-customer", status="completed")
    db = make_db(CUSTOMER_ROW, other_booking)
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 403
    assert "own bookings" in res.json()["detail"]


def test_returns_400_when_booking_not_completed():
    pending_booking = FakeRow(id="booking-1", customer_id="customer-1", status="confirmed")
    db = make_db(CUSTOMER_ROW, pending_booking)
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 400
    assert "completed" in res.json()["detail"]


def test_returns_400_when_review_already_exists():
    existing_review = FakeRow(id="existing-review")
    db = make_db(CUSTOMER_ROW, BOOKING_ROW, existing_review)
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 400
    assert "already been reviewed" in res.json()["detail"]


def test_returns_422_when_rating_out_of_range():
    db = make_db(CUSTOMER_ROW, BOOKING_ROW, NO_REVIEW_ROW, None, INSERTED_REVIEW)
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json={"booking_id": "booking-1", "rating": 6})
    assert res.status_code == 422


def test_creates_review_and_returns_response():
    db = make_db(CUSTOMER_ROW, BOOKING_ROW, NO_REVIEW_ROW, None, INSERTED_REVIEW)
    client = TestClient(make_test_app(db, CUSTOMER_USER))
    res = client.post("/api/reviews/", json=VALID_PAYLOAD)
    assert res.status_code == 200
    body = res.json()
    assert body["id"] == "review-1"
    assert body["rating"] == 5
    assert body["is_approved"] is True
