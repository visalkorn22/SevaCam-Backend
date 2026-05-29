from __future__ import annotations

from typing import Iterable, Optional


def normalize_staff_skills(skills: Optional[Iterable[str]]) -> list[str]:
    if not skills:
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_skill in skills:
        skill = str(raw_skill).strip()
        if not skill:
            continue
        key = skill.casefold()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(skill)
    return normalized


def round_average_rating(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return None


def calculate_experience_level(
    average_rating: object,
    completed_bookings: object,
) -> str:
    rating = round_average_rating(average_rating) or 0.0
    try:
        bookings = int(completed_bookings or 0)
    except (TypeError, ValueError):
        bookings = 0

    if rating >= 4.8 and bookings > 50:
        return "Expert"
    if rating >= 4.5 and bookings > 30:
        return "Experienced"
    if rating >= 3.5 and bookings >= 10:
        return "Intermediate"
    return "Beginner"
