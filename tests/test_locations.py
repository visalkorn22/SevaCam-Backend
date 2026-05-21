import os, sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from app.core.database import get_db
from app.core.auth import get_current_user

class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs

def make_db(fetchone_val=None):
    mock_db = MagicMock()
    result = MagicMock()
    result.fetchone.return_value = fetchone_val
    mock_db.execute.return_value = result
    return mock_db

def make_app(mock_db, mock_user):
    from app.api.admin import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user
    return app

def test_create_location_includes_lat_lng():
    created = FakeRow(
        id="loc-1", name="Main Branch", timezone="Asia/Phnom_Penh",
        address="123 St", latitude=11.5564, longitude=104.9282,
        is_active=True, created_at="2026-01-01T00:00:00"
    )
    db = make_db(created)
    user = {"id": "u1", "role": "admin"}
    client = TestClient(make_app(db, user))
    resp = client.post("/api/admin/locations", json={
        "name": "Main Branch",
        "address": "123 St",
        "latitude": 11.5564,
        "longitude": 104.9282,
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["latitude"] == 11.5564
    assert data["longitude"] == 104.9282

def test_update_location_lat_lng():
    updated = FakeRow(
        id="loc-1", name="Main Branch", timezone="Asia/Phnom_Penh",
        address="123 St", latitude=12.0, longitude=105.0,
        is_active=True, created_at="2026-01-01T00:00:00"
    )
    db = make_db(updated)
    db.execute.return_value.rowcount = 1
    user = {"id": "u1", "role": "admin"}
    client = TestClient(make_app(db, user))
    resp = client.put("/api/admin/locations/loc-1", json={"latitude": 12.0, "longitude": 105.0})
    assert resp.status_code == 200
    data = resp.json()
    assert data["latitude"] == 12.0

def test_booking_create_schema_has_location_id():
    from app.models.schemas import BookingCreate
    from datetime import datetime, timezone
    b = BookingCreate(
        service_id="s1",
        staff_id="st1",
        customer_id="c1",
        start_time_utc=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
        location_id="loc-1",
    )
    assert b.location_id == "loc-1"

def test_booking_create_schema_location_id_optional():
    from app.models.schemas import BookingCreate
    from datetime import datetime, timezone
    b = BookingCreate(
        service_id="s1",
        staff_id="st1",
        customer_id="c1",
        start_time_utc=datetime(2026, 5, 1, 10, 0, tzinfo=timezone.utc),
    )
    assert b.location_id is None
