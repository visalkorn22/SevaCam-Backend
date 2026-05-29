import os
import sys
from unittest.mock import MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")

from app.api.services import router as services_router
from app.core.database import get_db
from app.core.staff_profiles import (
    calculate_experience_level,
    normalize_staff_skills,
    round_average_rating,
)


class FakeTupleRow:
    def __init__(self, values: tuple):
        self._values = values

    def __getitem__(self, index: int):
        return self._values[index]


def make_service_staff_db(rows: list[FakeTupleRow]):
    mock_db = MagicMock()
    result = MagicMock()
    result.fetchall.return_value = rows
    mock_db.execute.return_value = result
    return mock_db


def make_test_app(mock_db):
    app = FastAPI()
    app.include_router(services_router, prefix="/api/services")
    app.dependency_overrides[get_db] = lambda: mock_db
    return app


def test_experience_level_thresholds():
    assert calculate_experience_level(4.9, 55) == "Expert"
    assert calculate_experience_level(4.7, 40) == "Experienced"
    assert calculate_experience_level(4.2, 18) == "Intermediate"
    assert calculate_experience_level(3.4, 40) == "Beginner"
    assert calculate_experience_level(None, 0) == "Beginner"


def test_skills_are_normalized():
    assert normalize_staff_skills([" Portrait ", "portrait", "", "Editing"]) == [
        "Portrait",
        "Editing",
    ]
    assert round_average_rating("4.84") == 4.8


def test_public_service_staff_returns_profile_and_computed_metrics():
    db = make_service_staff_db(
        [
            FakeTupleRow(
                (
                    "staff-1",
                    "Mr. Dara",
                    "/uploads/staff-1.webp",
                    None,
                    None,
                    None,
                    None,
                    None,
                    ["Portrait Photography", "Photo Editing"],
                    "Specialized in portraits and product sessions.",
                    45,
                    4.8,
                    19,
                )
            )
        ]
    )
    client = TestClient(make_test_app(db))

    response = client.get("/api/services/service-1/staff")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "Mr. Dara"
    assert body[0]["skills"] == ["Portrait Photography", "Photo Editing"]
    assert body[0]["bio"] == "Specialized in portraits and product sessions."
    assert body[0]["average_rating"] == 4.8
    assert body[0]["completed_bookings"] == 45
    assert body[0]["experience_level"] == "Experienced"
