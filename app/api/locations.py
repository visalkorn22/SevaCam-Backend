# backend/app/api/locations.py
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.schemas import LocationResponse

router = APIRouter(prefix="/api/locations", tags=["locations"])


def _serialize_location(row) -> dict:
    data = dict(row._mapping)
    data["id"] = str(data["id"])
    return data


@router.get("/", response_model=List[LocationResponse])
def list_locations(db: Session = Depends(get_db)):
    """Public endpoint that returns all active locations."""
    rows = db.execute(
        text("SELECT * FROM locations WHERE is_active = TRUE ORDER BY name")
    ).fetchall()
    return [_serialize_location(row) for row in rows]


@router.get("/{location_id}", response_model=LocationResponse)
def get_location(location_id: str, db: Session = Depends(get_db)):
    row = db.execute(
        text("SELECT * FROM locations WHERE id = :id"), {"id": location_id}
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Location not found")
    return _serialize_location(row)
