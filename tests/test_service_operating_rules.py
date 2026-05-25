import os
import sys
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ["DEBUG"] = "false"

from app.api.availability import (
    _get_service_operating_intervals,
    _is_nth_weekday_in_month,
)
from app.api.bookings import _service_allows_booking


class FakeRow:
    def __init__(self, **kwargs):
        self._mapping = kwargs


def _make_result(*, one=None, rows=None):
    result = MagicMock()
    result.fetchone.return_value = one
    result.fetchall.return_value = rows if rows is not None else []
    return result


def _make_db(*results):
    db = MagicMock()
    remaining = iter(results)

    def _execute(*args, **kwargs):
        try:
            return next(remaining)
        except StopIteration as exc:
            raise AssertionError("Unexpected database query during test") from exc

    db.execute.side_effect = _execute
    return db


def _fake_zoneinfo(key: str):
    if key == "UTC":
        return dt_timezone.utc
    if key == "Asia/Phnom_Penh":
        return dt_timezone(timedelta(hours=7))
    raise AssertionError(f"Unexpected timezone key in test: {key}")


def test_nth_weekday_uses_backend_sunday_zero_convention():
    assert _is_nth_weekday_in_month(date(2026, 6, 7), 0, 1) is True
    assert _is_nth_weekday_in_month(date(2026, 6, 7), 1, 1) is False


def test_service_operating_intervals_match_first_sunday_rule():
    db = _make_db(
        _make_result(
            one=FakeRow(
                id="schedule-1",
                timezone="Asia/Phnom_Penh",
                rule_type="monthly",
                open_time=time(9, 0),
                close_time=time(17, 0),
                effective_from=None,
                effective_to=None,
                is_active=True,
            )
        ),
        _make_result(rows=[]),
        _make_result(
            rows=[
                (
                    "monthly_nth_weekday",
                    0,
                    None,
                    1,
                    time(9, 0),
                    time(17, 0),
                )
            ]
        ),
    )

    with patch("app.api.availability.ZoneInfo", side_effect=_fake_zoneinfo):
        intervals = _get_service_operating_intervals(
            db=db,
            service_id="service-1",
            target_date=date(2026, 6, 7),
        )

    assert intervals == [
        (
            datetime(2026, 6, 7, 2, 0, tzinfo=dt_timezone.utc),
            datetime(2026, 6, 7, 10, 0, tzinfo=dt_timezone.utc),
        )
    ]


def test_service_allows_booking_for_first_sunday_rule():
    tz = dt_timezone(timedelta(hours=7))
    db = _make_db(
        _make_result(
            one=FakeRow(
                id="schedule-1",
                timezone="Asia/Phnom_Penh",
                rule_type="monthly",
                open_time=time(9, 0),
                close_time=time(17, 0),
                effective_from=None,
                effective_to=None,
                is_active=True,
            )
        ),
        _make_result(rows=[]),
        _make_result(
            rows=[
                (
                    "monthly_nth_weekday",
                    0,
                    None,
                    1,
                    time(9, 0),
                    time(17, 0),
                )
            ]
        ),
    )

    with patch("app.api.bookings.ZoneInfo", side_effect=_fake_zoneinfo):
        allowed = _service_allows_booking(
            db=db,
            service_id="service-1",
            local_start=datetime(2026, 6, 7, 10, 0, tzinfo=tz),
            local_end=datetime(2026, 6, 7, 11, 0, tzinfo=tz),
            schedule_tz=tz,
        )

    assert allowed is True


def test_service_rejects_non_matching_sunday_for_first_sunday_rule():
    tz = dt_timezone(timedelta(hours=7))
    db = _make_db(
        _make_result(
            one=FakeRow(
                id="schedule-1",
                timezone="Asia/Phnom_Penh",
                rule_type="monthly",
                open_time=time(9, 0),
                close_time=time(17, 0),
                effective_from=None,
                effective_to=None,
                is_active=True,
            )
        ),
        _make_result(rows=[]),
        _make_result(
            rows=[
                (
                    "monthly_nth_weekday",
                    0,
                    None,
                    1,
                    time(9, 0),
                    time(17, 0),
                )
            ]
        ),
    )

    with patch("app.api.bookings.ZoneInfo", side_effect=_fake_zoneinfo):
        allowed = _service_allows_booking(
            db=db,
            service_id="service-1",
            local_start=datetime(2026, 6, 14, 10, 0, tzinfo=tz),
            local_end=datetime(2026, 6, 14, 11, 0, tzinfo=tz),
            schedule_tz=tz,
        )

    assert allowed is False
