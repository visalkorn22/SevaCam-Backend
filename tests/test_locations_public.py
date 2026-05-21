import os, sys
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from app.core.database import get_db

class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs

def make_db_list(rows):
    mock_db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    mock_db.execute.return_value = result
    return mock_db

def make_app(mock_db):
    from app.api.locations import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: mock_db
    return app

def test_list_locations_public():
    rows = [FakeRow(
        id="loc-1", name="Main Branch", timezone="Asia/Phnom_Penh",
        address="123 St", latitude=11.5564, longitude=104.9282,
        is_active=True, created_at="2026-01-01T00:00:00"
    )]
    db = make_db_list(rows)
    client = TestClient(make_app(db))
    resp = client.get("/api/locations")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["name"] == "Main Branch"
    assert data[0]["latitude"] == 11.5564
