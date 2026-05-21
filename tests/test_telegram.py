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
    from app.api.telegram import router
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: mock_db
    app.dependency_overrides[get_current_user] = lambda: mock_user
    return app

def test_status_not_connected():
    db = make_db(fetchone_val=None)
    user = {"id": "user-1", "role": "customer"}
    client = TestClient(make_app(db, user))
    resp = client.get("/api/telegram/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False

def test_status_connected():
    row = FakeRow(id="t1", user_id="user-1", chat_id=123456789, created_at="2026-01-01")
    db = make_db(fetchone_val=row)
    user = {"id": "user-1", "role": "customer"}
    client = TestClient(make_app(db, user))
    resp = client.get("/api/telegram/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is True
