from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from typing import List, Optional, Dict, Tuple
from datetime import datetime, date, time, timedelta, timezone as dt_timezone
import calendar
import json
from time import time as now_ts
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from app.core.database import get_db
from app.core.auth import require_roles, is_admin
from app.core.audit import log_audit
from app.core.config import settings
from app.models.schemas import (
    AvailabilityRuleCreate, AvailabilityRuleResponse,
    AvailabilityExceptionCreate, AvailabilityExceptionResponse,
    AvailableSlot,
    StaffWeeklyScheduleCreate, StaffWeeklyScheduleResponse,
    StaffWeeklyScheduleUpdate,
    StaffWorkBlockCreate, StaffWorkBlockResponse,
    StaffBreakBlockCreate, StaffBreakBlockResponse,
    StaffExceptionCreate, StaffExceptionResponse,
    StaffExceptionBulkCreate,
    BookingHoldCreate, BookingHoldResponse,
    ScheduleChangeRequestCreate, ScheduleChangeRequestResponse,
    ScheduleChangeRequestReview
)
import uuid

router = APIRouter()

_SLOT_CACHE: Dict[str, Dict[str, object]] = {}
_SLOT_CACHE_TTL_SECONDS = 60
BOOKINGS_ENABLED = settings.FEATURE_SET == "full"
NOTIFICATIONS_ENABLED = settings.FEATURE_SET == "full"

def _normalize_schedule_request_row(row) -> dict:
    row_map = dict(row._mapping)
    payload_data = row_map.get("payload")
    if isinstance(payload_data, str):
        try:
            row_map["payload"] = json.loads(payload_data)
        except json.JSONDecodeError:
            row_map["payload"] = {}
    return _normalize_uuid_values(row_map)

def _normalize_uuid_values(row: dict) -> dict:
    for key, value in row.items():
        if isinstance(value, uuid.UUID):
            row[key] = str(value)
    return row

def _ensure_staff_or_admin(current_user: dict, staff_id: str) -> None:
    if is_admin(current_user):
        return
    if current_user.get("id") != staff_id:
        raise HTTPException(status_code=403, detail="Forbidden")

def _resolve_staff_id(current_user: dict, staff_id: Optional[str]) -> str:
    if staff_id in (None, "", "me"):
        return current_user.get("id")
    return staff_id

def _as_utc_aware(value: datetime) -> datetime:
    """Normalize DB datetime values to UTC-aware datetimes for safe comparisons."""
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)

def _merge_intervals(intervals: List[Tuple[datetime, datetime]]) -> List[Tuple[datetime, datetime]]:
    if not intervals:
        return []
    intervals = sorted(intervals, key=lambda x: x[0])
    merged = [intervals[0]]
    for start, end in intervals[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged

def _subtract_intervals(
    source: List[Tuple[datetime, datetime]],
    remove: List[Tuple[datetime, datetime]]
) -> List[Tuple[datetime, datetime]]:
    if not source:
        return []
    if not remove:
        return source
    remove = _merge_intervals(remove)
    result: List[Tuple[datetime, datetime]] = []
    for start, end in source:
        cursor = start
        for r_start, r_end in remove:
            if r_end <= cursor or r_start >= end:
                continue
            if r_start > cursor:
                result.append((cursor, r_start))
            cursor = max(cursor, r_end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
    return result

def _clip_interval(
    start: datetime,
    end: datetime,
    day_start: datetime,
    day_end: datetime
) -> Optional[Tuple[datetime, datetime]]:
    clipped_start = max(start, day_start)
    clipped_end = min(end, day_end)
    if clipped_start >= clipped_end:
        return None
    return (clipped_start, clipped_end)

def _intersect_intervals(
    left: List[Tuple[datetime, datetime]],
    right: List[Tuple[datetime, datetime]],
) -> List[Tuple[datetime, datetime]]:
    if not left or not right:
        return []
    left = _merge_intervals(left)
    right = _merge_intervals(right)
    result: List[Tuple[datetime, datetime]] = []
    for l_start, l_end in left:
        for r_start, r_end in right:
            start = max(l_start, r_start)
            end = min(l_end, r_end)
            if start < end:
                result.append((start, end))
    return _merge_intervals(result)

def _is_nth_weekday_in_month(target_date: date, weekday: int, nth: int) -> bool:
    if target_date.weekday() != weekday:
        return False
    first_day = target_date.replace(day=1)
    offset = (weekday - first_day.weekday()) % 7
    first_occurrence = 1 + offset
    occurrence = ((target_date.day - first_occurrence) // 7) + 1
    if nth == -1:
        last_day = calendar.monthrange(target_date.year, target_date.month)[1]
        last_date = target_date.replace(day=last_day)
        last_offset = (last_date.weekday() - weekday) % 7
        last_occurrence_day = last_day - last_offset
        return target_date.day == last_occurrence_day
    return occurrence == nth

def _get_service_operating_intervals(
    db: Session,
    service_id: str,
    target_date: date,
) -> List[Tuple[datetime, datetime]]:
    schedule = db.execute(
        text(
            """
            SELECT * FROM service_operating_schedules
            WHERE service_id = :service_id
              AND is_active = TRUE
              AND (effective_from IS NULL OR effective_from <= :date)
              AND (effective_to IS NULL OR effective_to >= :date)
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"service_id": service_id, "date": target_date},
    ).fetchone()

    utc = dt_timezone.utc
    service_tz = ZoneInfo("UTC")

    if schedule:
        schedule_map = schedule._mapping
        service_tz = ZoneInfo(schedule_map.get("timezone") or "UTC")

    service_day_start = datetime.combine(target_date, time(0, 0), tzinfo=service_tz)
    service_day_end = service_day_start + timedelta(days=1)
    service_weekday = (service_day_start.weekday() + 1) % 7

    if not schedule:
        return [(service_day_start.astimezone(utc), service_day_end.astimezone(utc))]

    exceptions = db.execute(
        text(
            """
            SELECT is_open, start_time, end_time
            FROM service_operating_exceptions
            WHERE service_id = :service_id AND date = :date
            """
        ),
        {"service_id": service_id, "date": target_date},
    ).fetchall()

    override_exceptions = [ex for ex in exceptions if ex[0] and ex[1] and ex[2]]
    closed_exceptions = [ex for ex in exceptions if not ex[0]]
    extra_open_exceptions = [ex for ex in exceptions if ex[0] and not (ex[1] and ex[2])]

    intervals: List[Tuple[datetime, datetime]] = []

    if override_exceptions:
        for ex in override_exceptions:
            start_dt = datetime.combine(target_date, ex[1], tzinfo=service_tz)
            end_dt = datetime.combine(target_date, ex[2], tzinfo=service_tz)
            intervals.append((start_dt, end_dt))
    elif closed_exceptions:
        return []
    elif extra_open_exceptions:
        intervals.append((service_day_start, service_day_end))
    else:
        schedule_map = schedule._mapping
        rule_type = schedule_map.get("rule_type")

        if rule_type == "daily":
            open_time = schedule_map.get("open_time")
            close_time = schedule_map.get("close_time")
            if open_time and close_time:
                intervals.append(
                    (
                        datetime.combine(target_date, open_time, tzinfo=service_tz),
                        datetime.combine(target_date, close_time, tzinfo=service_tz),
                    )
                )
            else:
                intervals.append((service_day_start, service_day_end))
        else:
            rules = db.execute(
                text(
                    """
                    SELECT rule_type, weekday, month_day, nth, start_time, end_time
                    FROM service_operating_rules
                    WHERE schedule_id = :schedule_id
                    """
                ),
                {"schedule_id": schedule_map.get("id")},
            ).fetchall()

            for rule in rules:
                rule_type_value = rule[0]
                weekday = rule[1]
                month_day = rule[2]
                nth = rule[3]
                start_time = rule[4]
                end_time = rule[5]

                is_match = False
                if rule_type == "weekly" and rule_type_value == "weekly":
                    is_match = weekday == service_weekday
                elif rule_type == "monthly" and rule_type_value == "monthly_day":
                    is_match = month_day == target_date.day
                elif rule_type == "monthly" and rule_type_value == "monthly_nth_weekday":
                    if weekday is not None and nth is not None:
                        is_match = _is_nth_weekday_in_month(target_date, weekday, nth)

                if not is_match:
                    continue

                if start_time and end_time:
                    intervals.append(
                        (
                            datetime.combine(target_date, start_time, tzinfo=service_tz),
                            datetime.combine(target_date, end_time, tzinfo=service_tz),
                        )
                    )
                elif schedule_map.get("open_time") and schedule_map.get("close_time"):
                    intervals.append(
                        (
                            datetime.combine(target_date, schedule_map["open_time"], tzinfo=service_tz),
                            datetime.combine(target_date, schedule_map["close_time"], tzinfo=service_tz),
                        )
                    )
                else:
                    intervals.append((service_day_start, service_day_end))

        if not intervals:
            return []

    converted = [
        (start.astimezone(utc), end.astimezone(utc)) for start, end in intervals
    ]
    return _merge_intervals(converted)

def _round_up_to_granularity(value: datetime, granularity_minutes: int) -> datetime:
    midnight = value.replace(hour=0, minute=0, second=0, microsecond=0)
    delta_minutes = int((value - midnight).total_seconds() // 60)
    remainder = delta_minutes % granularity_minutes
    if remainder == 0 and value.second == 0 and value.microsecond == 0:
        return value.replace(second=0, microsecond=0)
    increment = granularity_minutes - remainder
    return midnight + timedelta(minutes=delta_minutes + increment)

def _get_cached_slots(cache_key: str) -> Optional[List[dict]]:
    entry = _SLOT_CACHE.get(cache_key)
    if not entry:
        return None
    if now_ts() - float(entry["ts"]) > _SLOT_CACHE_TTL_SECONDS:
        _SLOT_CACHE.pop(cache_key, None)
        return None
    return entry.get("data")  # type: ignore

def _set_cached_slots(cache_key: str, data: List[dict]) -> None:
    _SLOT_CACHE[cache_key] = {"ts": now_ts(), "data": data}

def _validate_uuid_param(value: Optional[str], field_name: str) -> Optional[str]:
    if value in (None, ""):
        return None
    try:
        uuid.UUID(str(value))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid {field_name}")
    return value

def _compute_slots_for_date(
    db: Session,
    service_id: str,
    target_date: date,
    timezone: str,
    staff_id: Optional[str],
    location_id: Optional[str],
    granularity_minutes: int,
    window_start: Optional[time],
    window_end: Optional[time],
    min_notice_minutes: int,
    max_booking_days: int,
    ignore_booking_limits: bool = False,
) -> List[dict]:
    try:
        customer_tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    service_result = db.execute(
        text(
            "SELECT duration_minutes, buffer_minutes, max_capacity "
            "FROM services WHERE id = :id"
        ),
        {"id": service_id},
    )
    service = service_result.fetchone()
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")

    base_duration = int(service[0])
    base_buffer = int(service[1] or 0)
    base_capacity = int(service[2] or 1)

    staff_query = """
        SELECT ss.staff_id, u.full_name,
               ss.duration_override, ss.buffer_override, ss.capacity_override
        FROM staff_services ss
        JOIN users u ON u.id = ss.staff_id
        WHERE ss.service_id = :service_id
          AND ss.is_bookable = TRUE
          AND ss.is_temporarily_unavailable = FALSE
          AND ss.admin_only = FALSE
    """
    params: Dict[str, object] = {"service_id": service_id}
    if staff_id:
        staff_query += " AND ss.staff_id = :staff_id"
        params["staff_id"] = staff_id
    if location_id:
        staff_query += " AND u.location_id = :location_id"
        params["location_id"] = location_id

    staff_result = db.execute(text(staff_query), params)
    staff_rows = staff_result.fetchall()
    if not staff_rows:
        return []

    utc = dt_timezone.utc

    available_slots: List[dict] = []

    for row in staff_rows:
        staff_id = row[0]
        staff_id_str = str(staff_id)
        staff_name = row[1]
        duration = int(row[2] or base_duration)
        buffer_minutes = int(row[3] or base_buffer)
        capacity = int(row[4] or base_capacity or 1)
        total_minutes = duration + buffer_minutes

        schedule_query = """
            SELECT * FROM staff_weekly_schedules
            WHERE staff_id = :staff_id
              AND (effective_from IS NULL OR effective_from <= :date)
              AND (effective_to IS NULL OR effective_to >= :date)
        """
        schedule_params: Dict[str, object] = {"staff_id": staff_id, "date": target_date}
        if location_id:
            schedule_query += " AND (location_id = :location_id OR location_id IS NULL)"
            schedule_params["location_id"] = location_id
        schedule_query += " ORDER BY (location_id IS NULL) ASC, is_default DESC, effective_from DESC NULLS LAST"

        schedule = db.execute(text(schedule_query), schedule_params).fetchone()
        if not schedule:
            continue

        schedule_map = schedule._mapping
        schedule_tz = ZoneInfo(schedule_map["timezone"])
        max_slots_per_day = schedule_map.get("max_slots_per_day")
        max_bookings_per_day = schedule_map.get("max_bookings_per_day")
        day_start = datetime.combine(target_date, time(0, 0), tzinfo=schedule_tz)
        day_end = day_start + timedelta(days=1)

        now_local = datetime.now(schedule_tz)
        min_notice_cutoff = now_local + timedelta(minutes=min_notice_minutes)
        max_booking_cutoff = now_local + timedelta(days=max_booking_days)
        if day_start.date() > max_booking_cutoff.date():
            continue

        weekday = (day_start.weekday() + 1) % 7

        work_blocks = db.execute(
            text(
                """
                SELECT start_time_local, end_time_local
                FROM staff_work_blocks
                WHERE schedule_id = :schedule_id AND weekday = :weekday
                """
            ),
            {"schedule_id": schedule._mapping["id"], "weekday": weekday},
        ).fetchall()

        if not work_blocks:
            continue

        intervals: List[Tuple[datetime, datetime]] = []
        for block in work_blocks:
            start_dt = datetime.combine(target_date, block[0], tzinfo=schedule_tz)
            end_dt = datetime.combine(target_date, block[1], tzinfo=schedule_tz)
            if end_dt <= start_dt:
                continue
            intervals.append((start_dt, end_dt))

        break_blocks = db.execute(
            text(
                """
                SELECT start_time_local, end_time_local
                FROM staff_break_blocks
                WHERE schedule_id = :schedule_id AND weekday = :weekday
                """
            ),
            {"schedule_id": schedule._mapping["id"], "weekday": weekday},
        ).fetchall()

        break_intervals: List[Tuple[datetime, datetime]] = []
        for block in break_blocks:
            start_dt = datetime.combine(target_date, block[0], tzinfo=schedule_tz)
            end_dt = datetime.combine(target_date, block[1], tzinfo=schedule_tz)
            clipped = _clip_interval(start_dt, end_dt, day_start, day_end)
            if clipped:
                break_intervals.append(clipped)

        intervals = _subtract_intervals(_merge_intervals(intervals), break_intervals)

        if window_start and window_end:
            window_start_dt = datetime.combine(target_date, window_start, tzinfo=schedule_tz)
            window_end_dt = datetime.combine(target_date, window_end, tzinfo=schedule_tz)
            window_interval = _clip_interval(window_start_dt, window_end_dt, day_start, day_end)
            if window_interval:
                intervals = _subtract_intervals(
                    intervals,
                    _subtract_intervals([(day_start, day_end)], [window_interval])
                )
            else:
                intervals = []

        day_start_utc = day_start.astimezone(utc)
        day_end_utc = day_end.astimezone(utc)

        if BOOKINGS_ENABLED and max_bookings_per_day is not None and not ignore_booking_limits:
            booking_count = db.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM bookings
                    WHERE staff_id = :staff_id
                      AND start_time_utc < :day_end
                      AND end_time_utc > :day_start
                      AND status NOT IN ('cancelled', 'no-show')
                    """
                ),
                {"staff_id": staff_id, "day_start": day_start_utc, "day_end": day_end_utc},
            ).scalar()
            if booking_count is not None and int(booking_count) >= int(max_bookings_per_day):
                continue

        exceptions = db.execute(
            text(
                """
                SELECT type, start_utc, end_utc, is_all_day
                FROM staff_exceptions
                WHERE staff_id = :staff_id
                  AND start_utc < :day_end
                  AND end_utc > :day_start
                """
            ),
            {"staff_id": staff_id, "day_start": day_start_utc, "day_end": day_end_utc},
        ).fetchall()

        override_intervals: List[Tuple[datetime, datetime]] = []
        time_off_intervals: List[Tuple[datetime, datetime]] = []
        blocked_intervals: List[Tuple[datetime, datetime]] = []
        extra_intervals: List[Tuple[datetime, datetime]] = []

        for ex in exceptions:
            ex_type = ex[0]
            ex_start = ex[1].astimezone(schedule_tz)
            ex_end = ex[2].astimezone(schedule_tz)
            clipped = _clip_interval(ex_start, ex_end, day_start, day_end)
            if not clipped:
                continue
            if ex_type == "override_day":
                override_intervals.append(clipped)
            elif ex_type == "time_off":
                if ex[3]:
                    time_off_intervals.append((day_start, day_end))
                else:
                    time_off_intervals.append(clipped)
            elif ex_type == "blocked_time":
                blocked_intervals.append(clipped)
            elif ex_type == "extra_availability":
                extra_intervals.append(clipped)

        if override_intervals:
            intervals = _merge_intervals(override_intervals)

        if time_off_intervals:
            intervals = _subtract_intervals(intervals, _merge_intervals(time_off_intervals))

        if blocked_intervals:
            intervals = _subtract_intervals(intervals, _merge_intervals(blocked_intervals))

        if extra_intervals:
            intervals = _merge_intervals(intervals + extra_intervals)

        service_intervals_utc = _get_service_operating_intervals(
            db=db,
            service_id=service_id,
            target_date=target_date,
        )
        if not service_intervals_utc:
            continue

        staff_intervals_utc = [
            (start.astimezone(utc), end.astimezone(utc))
            for start, end in intervals
        ]
        intersected_utc = _intersect_intervals(
            staff_intervals_utc,
            service_intervals_utc,
        )
        if not intersected_utc:
            continue
        intervals = [
            (start.astimezone(schedule_tz), end.astimezone(schedule_tz))
            for start, end in intersected_utc
        ]

        if not intervals:
            continue

        booked_intervals: List[Tuple[datetime, datetime]] = []
        if BOOKINGS_ENABLED:
            bookings = db.execute(
                text(
                    """
                    SELECT service_id, start_time_utc, end_time_utc
                    FROM bookings
                    WHERE staff_id = :staff_id
                      AND start_time_utc < :day_end
                      AND end_time_utc > :day_start
                      AND status NOT IN ('cancelled', 'no-show')
                    """
                ),
                {"staff_id": staff_id, "day_start": day_start_utc, "day_end": day_end_utc},
            ).fetchall()
            booked_intervals = [
                (b[0], _as_utc_aware(b[1]), _as_utc_aware(b[2]))
                for b in bookings
            ]

        holds = db.execute(
            text(
                """
                SELECT service_id, start_utc, end_utc
                FROM booking_holds
                WHERE staff_id = :staff_id
                  AND expires_at_utc > NOW()
                  AND start_utc < :day_end
                  AND end_utc > :day_start
                """
            ),
            {"staff_id": staff_id, "day_start": day_start_utc, "day_end": day_end_utc},
        ).fetchall()
        hold_intervals = [
            (h[0], _as_utc_aware(h[1]), _as_utc_aware(h[2]))
            for h in holds
        ]

        if max_slots_per_day is not None and int(max_slots_per_day) <= 0:
            continue

        slots_added = 0
        slot_limit = int(max_slots_per_day) if max_slots_per_day is not None else None

        for start_dt, end_dt in intervals:
            cursor = _round_up_to_granularity(start_dt, granularity_minutes)
            while cursor + timedelta(minutes=total_minutes) <= end_dt:
                if cursor < min_notice_cutoff:
                    cursor += timedelta(minutes=granularity_minutes)
                    continue
                if cursor > max_booking_cutoff:
                    break

                slot_start_utc = cursor.astimezone(utc)
                slot_end_utc = (cursor + timedelta(minutes=total_minutes)).astimezone(utc)
                conflict = False
                same_count = 0
                for booked_service_id, booked_start, booked_end in booked_intervals:
                    if slot_start_utc < booked_end and slot_end_utc > booked_start:
                        if booked_service_id != service_id:
                            conflict = True
                            break
                        same_count += 1
                if not conflict:
                    for hold_service_id, hold_start, hold_end in hold_intervals:
                        if slot_start_utc < hold_end and slot_end_utc > hold_start:
                            if hold_service_id != service_id:
                                conflict = True
                                break
                            same_count += 1

                if not conflict:
                    if capacity <= 1:
                        conflict = same_count > 0
                    else:
                        conflict = same_count >= capacity

                if not conflict:
                    available_slots.append({
                        "start_time": cursor.astimezone(customer_tz),
                        "end_time": (cursor + timedelta(minutes=duration)).astimezone(customer_tz),
                        "staff_id": staff_id_str,
                        "staff_name": staff_name,
                    })
                    slots_added += 1
                    if slot_limit is not None and slots_added >= slot_limit:
                        break

                cursor += timedelta(minutes=granularity_minutes)

            if slot_limit is not None and slots_added >= slot_limit:
                break

    return available_slots

def _get_schedule_owner(db: Session, schedule_id: str) -> Optional[str]:
    result = db.execute(
        text("SELECT staff_id FROM staff_weekly_schedules WHERE id = :id"),
        {"id": schedule_id}
    ).fetchone()
    return result[0] if result else None

def _get_block_schedule(db: Session, table: str, block_id: str) -> Optional[str]:
    result = db.execute(
        text(f"SELECT schedule_id FROM {table} WHERE id = :id"),
        {"id": block_id}
    ).fetchone()
    return result[0] if result else None

def _validate_schedule_limits(
    max_slots_per_day: Optional[int],
    max_bookings_per_day: Optional[int],
    max_bookings_per_customer: Optional[int] = None,
) -> None:
    if max_slots_per_day is not None and int(max_slots_per_day) < 0:
        raise HTTPException(status_code=400, detail="Max slots per day must be >= 0")
    if max_bookings_per_day is not None and int(max_bookings_per_day) < 0:
        raise HTTPException(status_code=400, detail="Max bookings per day must be >= 0")
    if max_bookings_per_customer is not None and int(max_bookings_per_customer) < 0:
        raise HTTPException(
            status_code=400,
            detail="Max bookings per customer must be >= 0",
        )

def _validate_weekday_time_block(weekday: int, start_time: time, end_time: time) -> None:
    if weekday < 0 or weekday > 6:
        raise HTTPException(status_code=400, detail="weekday must be between 0 (Sunday) and 6 (Saturday)")
    if start_time >= end_time:
        raise HTTPException(status_code=400, detail="start_time_local must be before end_time_local")

@router.post("/weekly-schedules", response_model=StaffWeeklyScheduleResponse)
async def create_weekly_schedule(
    payload: StaffWeeklyScheduleCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create a weekly schedule for a staff member"""
    staff_id = _resolve_staff_id(current_user, payload.staff_id)
    _ensure_staff_or_admin(current_user, staff_id)
    _validate_schedule_limits(
        payload.max_slots_per_day,
        payload.max_bookings_per_day,
        payload.max_bookings_per_customer,
    )
    schedule_id = str(uuid.uuid4())

    db.execute(
        text(
            """
            INSERT INTO staff_weekly_schedules
                (id, staff_id, timezone, effective_from, effective_to, is_default, location_id,
                 max_slots_per_day, max_bookings_per_day, max_bookings_per_customer)
            VALUES
                (:id, :staff_id, :timezone, :effective_from, :effective_to, :is_default, :location_id,
                 :max_slots_per_day, :max_bookings_per_day, :max_bookings_per_customer)
            """
        ),
        {
            "id": schedule_id,
            "staff_id": staff_id,
            "timezone": payload.timezone,
            "effective_from": payload.effective_from,
            "effective_to": payload.effective_to,
            "is_default": payload.is_default,
            "location_id": payload.location_id,
            "max_slots_per_day": payload.max_slots_per_day,
            "max_bookings_per_day": payload.max_bookings_per_day,
            "max_bookings_per_customer": payload.max_bookings_per_customer,
        },
    )
    log_audit(
        db,
        current_user.get("id"),
        "create",
        "staff_weekly_schedule",
        schedule_id,
        payload.model_dump(),
    )
    db.commit()

    created = db.execute(
        text("SELECT * FROM staff_weekly_schedules WHERE id = :id"),
        {"id": schedule_id},
    ).fetchone()
    return _normalize_uuid_values(dict(created._mapping))

@router.get("/weekly-schedules/{staff_id}", response_model=List[StaffWeeklyScheduleResponse])
async def get_weekly_schedules(
    staff_id: str,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get weekly schedules for a staff member"""
    staff_id = _resolve_staff_id(current_user, staff_id)
    _ensure_staff_or_admin(current_user, staff_id)
    result = db.execute(
        text(
            """
            SELECT * FROM staff_weekly_schedules
            WHERE staff_id = :staff_id
            ORDER BY is_default DESC, effective_from NULLS FIRST
            """
        ),
        {"staff_id": staff_id},
    )
    return [_normalize_uuid_values(dict(row._mapping)) for row in result.fetchall()]

@router.patch("/weekly-schedules/{schedule_id}", response_model=StaffWeeklyScheduleResponse)
async def update_weekly_schedule(
    schedule_id: str,
    payload: StaffWeeklyScheduleUpdate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Update weekly schedule settings (Admin only)."""
    existing = db.execute(
        text("SELECT * FROM staff_weekly_schedules WHERE id = :id"),
        {"id": schedule_id},
    ).fetchone()
    if not existing:
        raise HTTPException(status_code=404, detail="Schedule not found")

    updates = payload.model_dump(exclude_unset=True)
    if updates:
        _validate_schedule_limits(
            updates.get("max_slots_per_day"),
            updates.get("max_bookings_per_day"),
            updates.get("max_bookings_per_customer"),
        )
        set_clause = ", ".join([f"{key} = :{key}" for key in updates.keys()])
        updates["id"] = schedule_id
        db.execute(
            text(f"UPDATE staff_weekly_schedules SET {set_clause} WHERE id = :id"),
            updates,
        )
        log_audit(
            db,
            current_user.get("id"),
            "update",
            "staff_weekly_schedule",
            schedule_id,
            updates,
        )
        db.commit()

    refreshed = db.execute(
        text("SELECT * FROM staff_weekly_schedules WHERE id = :id"),
        {"id": schedule_id},
    ).fetchone()
    return _normalize_uuid_values(dict(refreshed._mapping))

@router.delete("/weekly-schedules/{schedule_id}")
async def delete_weekly_schedule(
    schedule_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Delete a weekly schedule"""
    owner_id = _get_schedule_owner(db, schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)

    result = db.execute(
        text("DELETE FROM staff_weekly_schedules WHERE id = :id"),
        {"id": schedule_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "staff_weekly_schedule",
        schedule_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Schedule not found")

    return {"message": "Weekly schedule deleted"}

@router.post("/weekly-schedules/work-blocks", response_model=StaffWorkBlockResponse)
async def create_work_block(
    payload: StaffWorkBlockCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create a working block for a weekly schedule"""
    owner_id = _get_schedule_owner(db, payload.schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)
    _validate_weekday_time_block(payload.weekday, payload.start_time_local, payload.end_time_local)
    block_id = str(uuid.uuid4())

    try:
        db.execute(
            text(
                """
                INSERT INTO staff_work_blocks (id, schedule_id, weekday, start_time_local, end_time_local)
                VALUES (:id, :schedule_id, :weekday, :start_time_local, :end_time_local)
                """
            ),
            {
                "id": block_id,
                "schedule_id": payload.schedule_id,
                "weekday": payload.weekday,
                "start_time_local": payload.start_time_local,
                "end_time_local": payload.end_time_local,
            },
        )
        log_audit(
            db,
            current_user.get("id"),
            "create",
            "staff_work_block",
            block_id,
            payload.model_dump(),
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Failed to create work block. Check schedule_id and weekday/time constraints.",
        )

    created = db.execute(
        text("SELECT * FROM staff_work_blocks WHERE id = :id"),
        {"id": block_id},
    ).fetchone()
    return _normalize_uuid_values(dict(created._mapping))

@router.post("/weekly-schedules/break-blocks", response_model=StaffBreakBlockResponse)
async def create_break_block(
    payload: StaffBreakBlockCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create a break block for a weekly schedule"""
    owner_id = _get_schedule_owner(db, payload.schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)
    _validate_weekday_time_block(payload.weekday, payload.start_time_local, payload.end_time_local)
    block_id = str(uuid.uuid4())

    try:
        db.execute(
            text(
                """
                INSERT INTO staff_break_blocks (id, schedule_id, weekday, start_time_local, end_time_local)
                VALUES (:id, :schedule_id, :weekday, :start_time_local, :end_time_local)
                """
            ),
            {
                "id": block_id,
                "schedule_id": payload.schedule_id,
                "weekday": payload.weekday,
                "start_time_local": payload.start_time_local,
                "end_time_local": payload.end_time_local,
            },
        )
        log_audit(
            db,
            current_user.get("id"),
            "create",
            "staff_break_block",
            block_id,
            payload.model_dump(),
        )
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Failed to create break block. Check schedule_id and weekday/time constraints.",
        )

    created = db.execute(
        text("SELECT * FROM staff_break_blocks WHERE id = :id"),
        {"id": block_id},
    ).fetchone()
    return _normalize_uuid_values(dict(created._mapping))

@router.get("/weekly-schedules/{schedule_id}/blocks")
async def get_schedule_blocks(
    schedule_id: str,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get work and break blocks for a schedule"""
    owner_id = _get_schedule_owner(db, schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)

    work_blocks = db.execute(
        text(
            """
            SELECT * FROM staff_work_blocks
            WHERE schedule_id = :schedule_id
            ORDER BY weekday, start_time_local
            """
        ),
        {"schedule_id": schedule_id},
    ).fetchall()

    break_blocks = db.execute(
        text(
            """
            SELECT * FROM staff_break_blocks
            WHERE schedule_id = :schedule_id
            ORDER BY weekday, start_time_local
            """
        ),
        {"schedule_id": schedule_id},
    ).fetchall()

    return {
        "work_blocks": [_normalize_uuid_values(dict(row._mapping)) for row in work_blocks],
        "break_blocks": [_normalize_uuid_values(dict(row._mapping)) for row in break_blocks],
    }

@router.delete("/weekly-schedules/work-blocks/{block_id}")
async def delete_work_block(
    block_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Delete a work block"""
    schedule_id = _get_block_schedule(db, "staff_work_blocks", block_id)
    if not schedule_id:
        raise HTTPException(status_code=404, detail="Work block not found")

    owner_id = _get_schedule_owner(db, schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)

    result = db.execute(
        text("DELETE FROM staff_work_blocks WHERE id = :id"),
        {"id": block_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "staff_work_block",
        block_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Work block not found")

    return {"message": "Work block deleted"}

@router.delete("/weekly-schedules/break-blocks/{block_id}")
async def delete_break_block(
    block_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Delete a break block"""
    schedule_id = _get_block_schedule(db, "staff_break_blocks", block_id)
    if not schedule_id:
        raise HTTPException(status_code=404, detail="Break block not found")

    owner_id = _get_schedule_owner(db, schedule_id)
    if not owner_id:
        raise HTTPException(status_code=404, detail="Schedule not found")

    _ensure_staff_or_admin(current_user, owner_id)

    result = db.execute(
        text("DELETE FROM staff_break_blocks WHERE id = :id"),
        {"id": block_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "staff_break_block",
        block_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Break block not found")

    return {"message": "Break block deleted"}

@router.post("/staff-exceptions", response_model=StaffExceptionResponse)
async def create_staff_exception(
    payload: StaffExceptionCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create a staff exception (time off, blocked, extra, override)"""
    staff_id = _resolve_staff_id(current_user, payload.staff_id)
    _ensure_staff_or_admin(current_user, staff_id)
    exception_id = str(uuid.uuid4())

    db.execute(
        text(
            """
            INSERT INTO staff_exceptions
                (id, staff_id, location_id, type, start_utc, end_utc, is_all_day, recurring_rule, reason, created_by)
            VALUES
                (:id, :staff_id, :location_id, :type, :start_utc, :end_utc, :is_all_day, :recurring_rule, :reason, :created_by)
            """
        ),
        {
            "id": exception_id,
            "staff_id": staff_id,
            "location_id": payload.location_id,
            "type": payload.type,
            "start_utc": payload.start_utc,
            "end_utc": payload.end_utc,
            "is_all_day": payload.is_all_day,
            "recurring_rule": payload.recurring_rule,
            "reason": payload.reason,
            "created_by": current_user.get("id"),
        },
    )
    log_audit(
        db,
        current_user.get("id"),
        "create",
        "staff_exception",
        exception_id,
        payload.model_dump(),
    )
    db.commit()

    created = db.execute(
        text("SELECT * FROM staff_exceptions WHERE id = :id"),
        {"id": exception_id},
    ).fetchone()
    return _normalize_uuid_values(dict(created._mapping))

@router.post("/staff-exceptions/bulk", response_model=List[StaffExceptionResponse])
async def create_staff_exceptions_bulk(
    payload: StaffExceptionBulkCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Bulk create staff exceptions by staff list or location."""
    staff_ids: List[str] = []
    if payload.staff_ids:
        staff_ids = payload.staff_ids
    elif payload.location_id:
        staff_rows = db.execute(
            text(
                """
                SELECT id FROM users
                WHERE role = 'staff' AND location_id = :location_id
                """
            ),
            {"location_id": payload.location_id},
        ).fetchall()
        staff_ids = [row[0] for row in staff_rows]

    if not staff_ids:
        raise HTTPException(status_code=400, detail="No staff found for bulk exception")

    created_rows = []
    for staff_id in staff_ids:
        exception_id = str(uuid.uuid4())
        db.execute(
            text(
                """
                INSERT INTO staff_exceptions
                    (id, staff_id, location_id, type, start_utc, end_utc, is_all_day, recurring_rule, reason, created_by)
                VALUES
                    (:id, :staff_id, :location_id, :type, :start_utc, :end_utc, :is_all_day, :recurring_rule, :reason, :created_by)
                """
            ),
            {
                "id": exception_id,
                "staff_id": staff_id,
                "location_id": payload.location_id,
                "type": payload.type,
                "start_utc": payload.start_utc,
                "end_utc": payload.end_utc,
                "is_all_day": payload.is_all_day,
                "recurring_rule": payload.recurring_rule,
                "reason": payload.reason,
                "created_by": current_user.get("id"),
            },
        )
        log_audit(
            db,
            current_user.get("id"),
            "create",
            "staff_exception",
            exception_id,
            payload.model_dump(),
        )
        created_rows.append(exception_id)

    db.commit()

    results = db.execute(
        text("SELECT * FROM staff_exceptions WHERE id = ANY(:ids)"),
        {"ids": created_rows},
    ).fetchall()
    return [_normalize_uuid_values(dict(row._mapping)) for row in results]

@router.get("/staff-exceptions/{staff_id}", response_model=List[StaffExceptionResponse])
async def get_staff_exceptions(
    staff_id: str,
    start_utc: Optional[datetime] = None,
    end_utc: Optional[datetime] = None,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get staff exceptions with optional UTC range filter"""
    staff_id = _resolve_staff_id(current_user, staff_id)
    _ensure_staff_or_admin(current_user, staff_id)

    query = "SELECT * FROM staff_exceptions WHERE staff_id = :staff_id"
    params: Dict[str, object] = {"staff_id": staff_id}

    if start_utc:
        query += " AND end_utc >= :start_utc"
        params["start_utc"] = start_utc
    if end_utc:
        query += " AND start_utc <= :end_utc"
        params["end_utc"] = end_utc

    query += " ORDER BY start_utc"
    result = db.execute(text(query), params)
    return [_normalize_uuid_values(dict(row._mapping)) for row in result.fetchall()]

@router.delete("/staff-exceptions/{exception_id}")
async def delete_staff_exception(
    exception_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Delete a staff exception"""
    owner = db.execute(
        text("SELECT staff_id FROM staff_exceptions WHERE id = :id"),
        {"id": exception_id},
    ).fetchone()

    if not owner:
        raise HTTPException(status_code=404, detail="Exception not found")

    _ensure_staff_or_admin(current_user, owner[0])

    result = db.execute(
        text("DELETE FROM staff_exceptions WHERE id = :id"),
        {"id": exception_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "staff_exception",
        exception_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Exception not found")

    return {"message": "Exception deleted"}

@router.post("/holds", response_model=BookingHoldResponse)
async def create_booking_hold(
    payload: BookingHoldCreate,
    current_user: dict = Depends(require_roles("customer", "staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create a temporary hold for a slot."""
    hold_id = str(uuid.uuid4())
    db.execute(
        """
        INSERT INTO booking_holds
            (id, staff_id, service_id, location_id, start_utc, end_utc, expires_at_utc, created_by)
        VALUES
            (:id, :staff_id, :service_id, :location_id, :start_utc, :end_utc, :expires_at_utc, :created_by)
        """,
        {
            "id": hold_id,
            "staff_id": payload.staff_id,
            "service_id": payload.service_id,
            "location_id": payload.location_id,
            "start_utc": payload.start_utc,
            "end_utc": payload.end_utc,
            "expires_at_utc": payload.expires_at_utc,
            "created_by": current_user.get("id"),
        },
    )
    log_audit(
        db,
        current_user.get("id"),
        "create",
        "booking_hold",
        hold_id,
        payload.model_dump(),
    )
    db.commit()

    created = db.execute(
        "SELECT * FROM booking_holds WHERE id = :id",
        {"id": hold_id},
    ).fetchone()
    return _normalize_uuid_values(dict(created._mapping))

@router.get("/holds", response_model=List[BookingHoldResponse])
async def list_booking_holds(
    staff_id: Optional[str] = None,
    current_user: dict = Depends(require_roles("customer", "staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """List active booking holds."""
    query = "SELECT * FROM booking_holds WHERE expires_at_utc > NOW()"
    params: Dict[str, object] = {}

    if staff_id:
        query += " AND staff_id = :staff_id"
        params["staff_id"] = staff_id

    if current_user.get("role") == "customer":
        query += " AND created_by = :created_by"
        params["created_by"] = current_user.get("id")

    query += " ORDER BY expires_at_utc"
    result = db.execute(query, params)
    return [_normalize_uuid_values(dict(row._mapping)) for row in result.fetchall()]

@router.delete("/holds/{hold_id}")
async def delete_booking_hold(
    hold_id: str,
    current_user: dict = Depends(require_roles("customer", "staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Delete a booking hold."""
    hold = db.execute(
        "SELECT created_by FROM booking_holds WHERE id = :id",
        {"id": hold_id},
    ).fetchone()

    if not hold:
        raise HTTPException(status_code=404, detail="Hold not found")

    if not is_admin(current_user) and hold[0] != current_user.get("id"):
        raise HTTPException(status_code=403, detail="Forbidden")

    result = db.execute(
        "DELETE FROM booking_holds WHERE id = :id",
        {"id": hold_id},
    )
    log_audit(
        db,
        current_user.get("id"),
        "delete",
        "booking_hold",
        hold_id,
        None,
    )
    db.commit()

    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Hold not found")

    return {"message": "Hold deleted"}

@router.post("/rules", response_model=AvailabilityRuleResponse)
async def create_availability_rule(
    rule: AvailabilityRuleCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create availability rule for staff"""
    _ensure_staff_or_admin(current_user, rule.staff_id)
    rule_id = str(uuid.uuid4())
    
    db.execute(
        """
        INSERT INTO availability_rules (id, staff_id, service_id, day_of_week, 
                                       start_time, end_time, timezone)
        VALUES (:id, :staff_id, :service_id, :day_of_week, :start_time, :end_time, :timezone)
        """,
        {
            "id": rule_id,
            "staff_id": rule.staff_id,
            "service_id": rule.service_id,
            "day_of_week": rule.day_of_week,
            "start_time": rule.start_time,
            "end_time": rule.end_time,
            "timezone": rule.timezone,
        }
    )
    db.commit()
    
    result = db.execute(
        "SELECT * FROM availability_rules WHERE id = :id",
        {"id": rule_id}
    )
    return _normalize_uuid_values(dict(result.fetchone()._mapping))

@router.get("/rules/{staff_id}", response_model=List[AvailabilityRuleResponse])
async def get_staff_availability_rules(
    staff_id: str,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Get all availability rules for a staff member"""
    _ensure_staff_or_admin(current_user, staff_id)
    result = db.execute(
        "SELECT * FROM availability_rules WHERE staff_id = :staff_id ORDER BY day_of_week, start_time",
        {"staff_id": staff_id}
    )
    
    rules = result.fetchall()
    return [_normalize_uuid_values(dict(row._mapping)) for row in rules]

@router.delete("/rules/{rule_id}")
async def delete_availability_rule(
    rule_id: str,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Delete an availability rule"""
    rule_owner = db.execute(
        "SELECT staff_id FROM availability_rules WHERE id = :id",
        {"id": rule_id},
    ).fetchone()

    if not rule_owner:
        raise HTTPException(status_code=404, detail="Rule not found")

    _ensure_staff_or_admin(current_user, rule_owner[0])

    result = db.execute(
        "DELETE FROM availability_rules WHERE id = :id",
        {"id": rule_id}
    )
    db.commit()
    
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Rule not found")
    
    return {"message": "Availability rule deleted"}

@router.post("/exceptions", response_model=AvailabilityExceptionResponse)
async def create_availability_exception(
    exception: AvailabilityExceptionCreate,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Create availability exception (holiday, blocked time, etc.)"""
    _ensure_staff_or_admin(current_user, exception.staff_id)
    exception_id = str(uuid.uuid4())
    
    db.execute(
        """
        INSERT INTO availability_exceptions (id, staff_id, service_id, date, 
                                            is_available, start_time, end_time, reason)
        VALUES (:id, :staff_id, :service_id, :date, :is_available, :start_time, :end_time, :reason)
        """,
        {
            "id": exception_id,
            "staff_id": exception.staff_id,
            "service_id": exception.service_id,
            "date": exception.date,
            "is_available": exception.is_available,
            "start_time": exception.start_time,
            "end_time": exception.end_time,
            "reason": exception.reason,
        }
    )
    db.commit()
    
    result = db.execute(
        "SELECT * FROM availability_exceptions WHERE id = :id",
        {"id": exception_id}
    )
    return _normalize_uuid_values(dict(result.fetchone()._mapping))

@router.get("/exceptions/{staff_id}", response_model=List[AvailabilityExceptionResponse])
async def get_staff_availability_exceptions(
    staff_id: str,
    start_date: date = None,
    end_date: date = None,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db)
):
    """Get availability exceptions for a staff member"""
    _ensure_staff_or_admin(current_user, staff_id)
    query = "SELECT * FROM availability_exceptions WHERE staff_id = :staff_id"
    params = {"staff_id": staff_id}
    
    if start_date:
        query += " AND date >= :start_date"
        params["start_date"] = start_date
    if end_date:
        query += " AND date <= :end_date"
        params["end_date"] = end_date
    
    query += " ORDER BY date"
    
    result = db.execute(query, params)
    exceptions = result.fetchall()
    return [_normalize_uuid_values(dict(row._mapping)) for row in exceptions]

@router.get("/slots", response_model=List[AvailableSlot])
async def get_available_slots(
    service_id: str,
    date: date,
    staff_id: str = None,
    db: Session = Depends(get_db)
):
    """Get available time slots for a service on a specific date"""
    # Get service details
    service_result = db.execute(
        "SELECT duration_minutes, buffer_minutes, max_capacity FROM services WHERE id = :id",
        {"id": service_id}
    )
    service = service_result.fetchone()
    
    if not service:
        raise HTTPException(status_code=404, detail="Service not found")
    
    duration = service[0]
    buffer = service[1]
    base_capacity = int(service[2] or 1)
    total_slot_time = duration + buffer
    
    # Get staff assigned to this service
    staff_query = (
        "SELECT staff_id, capacity_override "
        "FROM staff_services "
        "WHERE service_id = :service_id "
        "AND is_bookable = TRUE "
        "AND is_temporarily_unavailable = FALSE "
        "AND admin_only = FALSE"
    )
    params = {"service_id": service_id}
    
    if staff_id:
        staff_query += " AND staff_id = :staff_id"
        params["staff_id"] = staff_id
    
    staff_result = db.execute(staff_query, params)
    staff_rows = staff_result.fetchall()
    staff_ids = [row[0] for row in staff_rows]
    capacity_by_staff = {
        row[0]: int(row[1] or base_capacity or 1) for row in staff_rows
    }
    
    if not staff_ids:
        return []
    
    # Get day of week (0 = Sunday)
    day_of_week = date.weekday()
    if day_of_week == 6:  # Python's Monday=0, Sunday=6
        day_of_week = 0
    else:
        day_of_week += 1
    
    available_slots = []
    service_intervals_utc = _get_service_operating_intervals(
        db=db,
        service_id=service_id,
        target_date=date,
    )
    if not service_intervals_utc:
        return []
    
    for staff_id in staff_ids:
        staff_capacity = capacity_by_staff.get(staff_id, base_capacity)
        schedule_limits = db.execute(
            """
            SELECT max_slots_per_day, max_bookings_per_day
            FROM staff_weekly_schedules
            WHERE staff_id = :staff_id
              AND (effective_from IS NULL OR effective_from <= :date)
              AND (effective_to IS NULL OR effective_to >= :date)
            ORDER BY is_default DESC, effective_from DESC NULLS LAST
            LIMIT 1
            """,
            {"staff_id": staff_id, "date": date},
        ).fetchone()
        max_slots_per_day = schedule_limits[0] if schedule_limits else None
        max_bookings_per_day = schedule_limits[1] if schedule_limits else None

        if max_slots_per_day is not None and int(max_slots_per_day) <= 0:
            continue

        # Get availability rules for this staff on this day
        rules_result = db.execute(
            """
            SELECT start_time, end_time, timezone
            FROM availability_rules
            WHERE staff_id = :staff_id AND day_of_week = :day_of_week
            """,
            {"staff_id": staff_id, "day_of_week": day_of_week}
        )
        rules = rules_result.fetchall()
        
        # Check for exceptions on this date
        exception_result = db.execute(
            """
            SELECT is_available, start_time, end_time
            FROM availability_exceptions
            WHERE staff_id = :staff_id AND date = :date
            """,
            {"staff_id": staff_id, "date": date}
        )
        exception = exception_result.fetchone()
        
        # If there's an exception and staff is not available, skip
        if exception and not exception[0]:
            continue
        
        # Use exception times if available, otherwise use rules
        time_ranges = []
        if exception and exception[1] and exception[2]:
            rule_timezone = rules[0][2] if rules else "UTC"
            time_ranges = [(exception[1], exception[2], rule_timezone)]
        else:
            time_ranges = [(rule[0], rule[1], rule[2]) for rule in rules]
        
        # Get existing bookings for this staff on this date
        booked_times: List[Tuple[datetime, datetime]] = []
        if BOOKINGS_ENABLED:
            bookings_result = db.execute(
                """
                SELECT service_id, start_time_utc, end_time_utc
                FROM bookings
                WHERE staff_id = :staff_id 
                AND DATE(start_time_utc) = :date
                AND status NOT IN ('cancelled', 'no-show')
                """,
                {"staff_id": staff_id, "date": date}
            )
            booked_times = [(row[0], row[1], row[2]) for row in bookings_result.fetchall()]

        if max_bookings_per_day is not None and len(booked_times) >= int(max_bookings_per_day):
            continue

        slots_added = 0
        slot_limit = int(max_slots_per_day) if max_slots_per_day is not None else None
        
        # Generate available slots
        for start_time, end_time, rule_timezone in time_ranges:
            rule_tz = ZoneInfo(rule_timezone or "UTC")
            current_time = datetime.combine(date, start_time, tzinfo=rule_tz)
            end_datetime = datetime.combine(date, end_time, tzinfo=rule_tz)
            
            while current_time + timedelta(minutes=total_slot_time) <= end_datetime:
                slot_end = current_time + timedelta(minutes=duration)
                slot_start_utc = current_time.astimezone(dt_timezone.utc)
                slot_end_utc = slot_end.astimezone(dt_timezone.utc)

                within_service = any(
                    slot_start_utc >= service_start
                    and slot_end_utc <= service_end
                    for service_start, service_end in service_intervals_utc
                )
                if not within_service:
                    current_time += timedelta(minutes=total_slot_time)
                    continue
                
                # Check if slot conflicts with existing bookings
                conflict = False
                same_count = 0
                for booked_service_id, booked_start, booked_end in booked_times:
                    if slot_start_utc < booked_end and slot_end_utc > booked_start:
                        if booked_service_id != service_id:
                            conflict = True
                            break
                        same_count += 1

                if not conflict:
                    if staff_capacity <= 1:
                        conflict = same_count > 0
                    else:
                        conflict = same_count >= staff_capacity

                if not conflict:
                    available_slots.append({
                        "start_time": current_time,
                        "end_time": slot_end,
                        "staff_id": staff_id,
                        "staff_name": None  # Can be joined from user_profiles
                    })
                    slots_added += 1
                    if slot_limit is not None and slots_added >= slot_limit:
                        break
                
                current_time += timedelta(minutes=total_slot_time)

            if slot_limit is not None and slots_added >= slot_limit:
                break
    
    return available_slots

@router.get("/slots-v2", response_model=List[AvailableSlot])
async def get_available_slots_v2(
    service_id: str,
    date: date,
    timezone: str,
    staff_id: str = None,
    location_id: str = None,
    granularity_minutes: Optional[int] = None,
    window_start: Optional[time] = None,
    window_end: Optional[time] = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db)
):
    """Get available time slots using weekly schedules + exceptions (timezone-aware)."""
    granularity = granularity_minutes or settings.SLOT_GRANULARITY_MINUTES
    if granularity not in (5, 10, 15, 30):
        raise HTTPException(status_code=400, detail="Invalid granularity")

    service_id = _validate_uuid_param(service_id, "service_id") or service_id
    staff_id = _validate_uuid_param(staff_id, "staff_id")
    location_id = _validate_uuid_param(location_id, "location_id")

    if staff_id:
        staff_exists = db.execute(
            text("SELECT 1 FROM users WHERE id = :id"),
            {"id": staff_id},
        ).fetchone()
        if not staff_exists:
            raise HTTPException(status_code=404, detail="Staff not found")

    if location_id:
        location_exists = db.execute(
            text("SELECT 1 FROM locations WHERE id = :id"),
            {"id": location_id},
        ).fetchone()
        if not location_exists:
            raise HTTPException(status_code=404, detail="Location not found")

    cache_key = (
        f"slots-v2:{service_id}:{date}:{timezone}:{staff_id or 'any'}:"
        f"{location_id or 'any'}:{granularity}:{window_start}:{window_end}:{limit}:{offset}"
    )
    cached = _get_cached_slots(cache_key)
    if cached is not None:
        return cached

    slots = _compute_slots_for_date(
        db=db,
        service_id=service_id,
        target_date=date,
        timezone=timezone,
        staff_id=staff_id,
        location_id=location_id,
        granularity_minutes=granularity,
        window_start=window_start,
        window_end=window_end,
        min_notice_minutes=settings.MIN_NOTICE_MINUTES,
        max_booking_days=settings.MAX_BOOKING_DAYS,
    )

    slots = sorted(slots, key=lambda s: s["start_time"])
    if offset:
        slots = slots[offset:]
    if limit:
        slots = slots[:limit]

    _set_cached_slots(cache_key, slots)
    return slots

@router.get("/slots-v2/next-available")
async def get_next_available_day(
    service_id: str,
    timezone: str,
    staff_id: str = None,
    location_id: str = None,
    granularity_minutes: Optional[int] = None,
    from_date: Optional[date] = None,
    db: Session = Depends(get_db)
):
    """Return the next available date with at least one slot."""
    granularity = granularity_minutes or settings.SLOT_GRANULARITY_MINUTES
    if granularity not in (5, 10, 15, 30):
        raise HTTPException(status_code=400, detail="Invalid granularity")

    service_id = _validate_uuid_param(service_id, "service_id") or service_id
    staff_id = _validate_uuid_param(staff_id, "staff_id")
    location_id = _validate_uuid_param(location_id, "location_id")

    if staff_id:
        staff_exists = db.execute(
            text("SELECT 1 FROM users WHERE id = :id"),
            {"id": staff_id},
        ).fetchone()
        if not staff_exists:
            raise HTTPException(status_code=404, detail="Staff not found")

    if location_id:
        location_exists = db.execute(
            text("SELECT 1 FROM locations WHERE id = :id"),
            {"id": location_id},
        ).fetchone()
        if not location_exists:
            raise HTTPException(status_code=404, detail="Location not found")

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")
    today = datetime.now(tz).date()
    start_date = from_date or today
    if start_date < today:
        start_date = today
    for day_offset in range(0, settings.MAX_BOOKING_DAYS + 1):
        target_date = start_date + timedelta(days=day_offset)
        cache_key = (
            f"slots-v2:{service_id}:{target_date}:{timezone}:{staff_id or 'any'}:"
            f"{location_id or 'any'}:{granularity}:None:None"
        )
        cached = _get_cached_slots(cache_key)
        if cached is None:
            slots = _compute_slots_for_date(
                db=db,
                service_id=service_id,
                target_date=target_date,
                timezone=timezone,
                staff_id=staff_id,
                location_id=location_id,
                granularity_minutes=granularity,
                window_start=None,
                window_end=None,
                min_notice_minutes=settings.MIN_NOTICE_MINUTES,
                max_booking_days=settings.MAX_BOOKING_DAYS,
            )
            _set_cached_slots(cache_key, slots)
        else:
            slots = cached
        if slots:
            return {"date": target_date}

    return {"date": None}

@router.get("/slots-v2/month")
async def get_month_availability(
    service_id: str,
    year: int,
    month: int,
    timezone: str,
    staff_id: str = None,
    location_id: str = None,
    granularity_minutes: Optional[int] = None,
    db: Session = Depends(get_db),
):
    """Return availability per day for a given month (customer booking calendar)."""
    if month < 1 or month > 12:
        raise HTTPException(status_code=400, detail="Invalid month")

    service_id = _validate_uuid_param(service_id, "service_id") or service_id
    staff_id = _validate_uuid_param(staff_id, "staff_id")
    location_id = _validate_uuid_param(location_id, "location_id")

    if staff_id:
        staff_exists = db.execute(
            text("SELECT 1 FROM users WHERE id = :id"),
            {"id": staff_id},
        ).fetchone()
        if not staff_exists:
            raise HTTPException(status_code=404, detail="Staff not found")

    if location_id:
        location_exists = db.execute(
            text("SELECT 1 FROM locations WHERE id = :id"),
            {"id": location_id},
        ).fetchone()
        if not location_exists:
            raise HTTPException(status_code=404, detail="Location not found")

    granularity = granularity_minutes or settings.SLOT_GRANULARITY_MINUTES
    if granularity not in (5, 10, 15, 30):
        raise HTTPException(status_code=400, detail="Invalid granularity")

    try:
        tz = ZoneInfo(timezone)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid timezone")

    today = datetime.now(tz).date()
    last_day = calendar.monthrange(year, month)[1]
    start_date = date(year, month, 1)
    end_date = date(year, month, last_day)
    max_date = today + timedelta(days=settings.MAX_BOOKING_DAYS)

    results = []
    current = start_date
    while current <= end_date:
        if current < today or current > max_date:
            results.append({"date": current, "has_slots": False})
            current += timedelta(days=1)
            continue

        cache_key = (
            f"slots-v2:{service_id}:{current}:{timezone}:{staff_id or 'any'}:"
            f"{location_id or 'any'}:{granularity}:None:None"
        )
        cached = _get_cached_slots(cache_key)
        if cached is None:
            slots = _compute_slots_for_date(
                db=db,
                service_id=service_id,
                target_date=current,
                timezone=timezone,
                staff_id=staff_id,
                location_id=location_id,
                granularity_minutes=granularity,
                window_start=None,
                window_end=None,
                min_notice_minutes=settings.MIN_NOTICE_MINUTES,
                max_booking_days=settings.MAX_BOOKING_DAYS,
            )
            _set_cached_slots(cache_key, slots)
        else:
            slots = cached

        results.append({"date": current, "has_slots": bool(slots)})
        current += timedelta(days=1)

    return results

@router.get("/calendar")
async def get_availability_calendar(
    start_date: date,
    end_date: date,
    staff_id: Optional[str] = None,
    location_id: Optional[str] = None,
    timezone: Optional[str] = None,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Return bookings, exceptions, and holds for availability calendar views."""
    if not is_admin(current_user):
        if staff_id and staff_id != current_user.get("id"):
            raise HTTPException(status_code=403, detail="Forbidden")
        staff_id = current_user.get("id")

    try:
        viewer_timezone = ZoneInfo(
            timezone or current_user.get("timezone") or "UTC",
        )
    except ZoneInfoNotFoundError:
        viewer_timezone = dt_timezone.utc

    start_utc = datetime.combine(
        start_date,
        time(0, 0),
        tzinfo=viewer_timezone,
    ).astimezone(dt_timezone.utc)
    end_utc = datetime.combine(
        end_date + timedelta(days=1),
        time(0, 0),
        tzinfo=viewer_timezone,
    ).astimezone(dt_timezone.utc)

    bookings = []
    if BOOKINGS_ENABLED:
        booking_query = """
            SELECT b.id, b.start_time_utc, b.end_time_utc, b.status,
                   s.name AS service_name,
                   u.id AS staff_id, u.full_name AS staff_name,
                   c.full_name AS customer_name
            FROM bookings b
            LEFT JOIN services s ON b.service_id = s.id
            LEFT JOIN users u ON b.staff_id = u.id
            LEFT JOIN customers c ON b.customer_id = c.id
            WHERE b.start_time_utc < :end_utc AND b.end_time_utc > :start_utc
        """
        booking_params: Dict[str, object] = {"start_utc": start_utc, "end_utc": end_utc}
        if staff_id:
            booking_query += " AND b.staff_id = :staff_id"
            booking_params["staff_id"] = staff_id
        if location_id:
            booking_query += " AND u.location_id = :location_id"
            booking_params["location_id"] = location_id

        bookings = db.execute(booking_query, booking_params).fetchall()

    exception_query = """
        SELECT id, type, start_utc, end_utc, reason, staff_id
        FROM staff_exceptions
        WHERE start_utc < :end_utc AND end_utc > :start_utc
    """
    exception_params: Dict[str, object] = {"start_utc": start_utc, "end_utc": end_utc}
    if staff_id:
        exception_query += " AND staff_id = :staff_id"
        exception_params["staff_id"] = staff_id
    if location_id:
        exception_query += " AND (location_id = :location_id OR location_id IS NULL)"
        exception_params["location_id"] = location_id

    exceptions = db.execute(exception_query, exception_params).fetchall()

    hold_query = """
        SELECT id, start_utc, end_utc, staff_id
        FROM booking_holds
        WHERE start_utc < :end_utc AND end_utc > :start_utc
          AND expires_at_utc > NOW()
    """
    hold_params: Dict[str, object] = {"start_utc": start_utc, "end_utc": end_utc}
    if staff_id:
        hold_query += " AND staff_id = :staff_id"
        hold_params["staff_id"] = staff_id
    if location_id:
        hold_query += " AND (location_id = :location_id OR location_id IS NULL)"
        hold_params["location_id"] = location_id

    holds = db.execute(hold_query, hold_params).fetchall()

    staff_ids: List[str] = []
    if staff_id:
        staff_ids = [staff_id]
    else:
        staff_query = "SELECT id FROM users WHERE role = 'staff'"
        staff_params: Dict[str, object] = {}
        if location_id:
            staff_query += " AND location_id = :location_id"
            staff_params["location_id"] = location_id
        staff_rows = db.execute(staff_query, staff_params).fetchall()
        staff_ids = [row[0] for row in staff_rows]

    utilization_by_day = []
    if BOOKINGS_ENABLED and staff_ids:
        current_date = start_date
        while current_date <= end_date:
            for staff_row_id in staff_ids:
                schedule = db.execute(
                    """
                    SELECT id, timezone
                    FROM staff_weekly_schedules
                    WHERE staff_id = :staff_id
                      AND (effective_from IS NULL OR effective_from <= :date)
                      AND (effective_to IS NULL OR effective_to >= :date)
                    ORDER BY is_default DESC, effective_from DESC NULLS LAST
                    LIMIT 1
                    """,
                    {"staff_id": staff_row_id, "date": current_date},
                ).fetchone()

                if not schedule:
                    continue

                schedule_tz = ZoneInfo(schedule[1])
                day_start_local = datetime.combine(current_date, time(0, 0), tzinfo=schedule_tz)
                day_end_local = day_start_local + timedelta(days=1)
                day_start_utc = day_start_local.astimezone(dt_timezone.utc)
                day_end_utc = day_end_local.astimezone(dt_timezone.utc)

                weekday = (day_start_local.weekday() + 1) % 7
                work_blocks = db.execute(
                    """
                    SELECT start_time_local, end_time_local
                    FROM staff_work_blocks
                    WHERE schedule_id = :schedule_id AND weekday = :weekday
                    """,
                    {"schedule_id": schedule[0], "weekday": weekday},
                ).fetchall()

                work_minutes = 0
                for block in work_blocks:
                    start_dt = datetime.combine(current_date, block[0], tzinfo=schedule_tz)
                    end_dt = datetime.combine(current_date, block[1], tzinfo=schedule_tz)
                    if end_dt > start_dt:
                        work_minutes += int((end_dt - start_dt).total_seconds() // 60)

                if work_minutes == 0:
                    continue

                booked_rows = db.execute(
                    """
                    SELECT start_time_utc, end_time_utc
                    FROM bookings
                    WHERE staff_id = :staff_id
                      AND status NOT IN ('cancelled', 'no-show')
                      AND start_time_utc < :day_end
                      AND end_time_utc > :day_start
                    """,
                    {
                        "staff_id": staff_row_id,
                        "day_start": day_start_utc,
                        "day_end": day_end_utc,
                    },
                ).fetchall()

                booked_minutes = 0
                for row in booked_rows:
                    overlap_start = max(row[0], day_start_utc)
                    overlap_end = min(row[1], day_end_utc)
                    if overlap_end > overlap_start:
                        booked_minutes += int((overlap_end - overlap_start).total_seconds() // 60)

                utilization = booked_minutes / work_minutes if work_minutes else 0
                utilization_by_day.append(
                    {
                        "date": current_date,
                        "staff_id": staff_row_id,
                        "work_minutes": work_minutes,
                        "booked_minutes": booked_minutes,
                        "utilization": utilization,
                    }
                )

            current_date += timedelta(days=1)

    conflicts = []
    if BOOKINGS_ENABLED:
        conflict_query = """
            SELECT b.id AS booking_id, e.id AS exception_id,
                   b.start_time_utc, b.end_time_utc, e.type,
                   u.id AS staff_id, u.full_name AS staff_name
            FROM bookings b
            JOIN staff_exceptions e ON e.staff_id = b.staff_id
            JOIN users u ON u.id = b.staff_id
            WHERE e.type IN ('time_off', 'blocked_time')
              AND b.status NOT IN ('cancelled', 'no-show')
              AND b.start_time_utc < :end_utc AND b.end_time_utc > :start_utc
              AND e.start_utc < :end_utc AND e.end_utc > :start_utc
              AND b.start_time_utc < e.end_utc AND b.end_time_utc > e.start_utc
        """
        conflict_params: Dict[str, object] = {"start_utc": start_utc, "end_utc": end_utc}
        if staff_id:
            conflict_query += " AND b.staff_id = :staff_id"
            conflict_params["staff_id"] = staff_id
        if location_id:
            conflict_query += " AND u.location_id = :location_id"
            conflict_params["location_id"] = location_id

        conflicts = db.execute(conflict_query, conflict_params).fetchall()

    items = []
    for row in bookings:
        items.append(
            {
                "id": str(row[0]),
                "type": "booking",
                "start_utc": row[1],
                "end_utc": row[2],
                "status": row[3],
                "service_name": row[4],
                "staff_id": row[5],
                "staff_name": row[6],
                "customer_name": row[7],
                "title": row[4] or "Booking",
            }
        )

    for row in exceptions:
        items.append(
            {
                "id": str(row[0]),
                "type": "exception",
                "exception_type": row[1],
                "start_utc": row[2],
                "end_utc": row[3],
                "reason": row[4],
                "staff_id": row[5],
                "title": row[1].replace("_", " ").title(),
            }
        )

    for row in holds:
        items.append(
            {
                "id": str(row[0]),
                "type": "hold",
                "start_utc": row[1],
                "end_utc": row[2],
                "staff_id": row[3],
                "title": "Hold",
            }
        )

    warnings = [
        {
            "booking_id": str(row[0]),
            "exception_id": str(row[1]),
            "start_utc": row[2],
            "end_utc": row[3],
            "exception_type": row[4],
            "staff_id": row[5],
            "staff_name": row[6],
            "message": "Booking overlaps staff exception",
        }
        for row in conflicts
    ]

    return {
        "items": sorted(items, key=lambda item: item["start_utc"]),
        "summary": {
            "bookings": len(bookings),
            "exceptions": len(exceptions),
            "holds": len(holds),
            "conflicts": len(conflicts),
        },
        "warnings": warnings,
        "utilization_by_day": utilization_by_day,
    }

@router.post("/schedule-requests", response_model=ScheduleChangeRequestResponse)
async def create_schedule_change_request(
    payload: ScheduleChangeRequestCreate,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Submit a schedule change request for approval."""
    staff_id = _resolve_staff_id(current_user, payload.staff_id)
    _ensure_staff_or_admin(current_user, staff_id)
    if not is_admin(current_user):
        payload_data = payload.payload or {}
        target = payload_data.get("target")
        action = payload_data.get("action")
        ex_type = payload_data.get("type")
        start_utc = payload_data.get("start_utc")
        end_utc = payload_data.get("end_utc")

        if target != "exception" or action != "add":
            raise HTTPException(
                status_code=403,
                detail="Staff can only request time off.",
            )
        if ex_type != "time_off":
            raise HTTPException(
                status_code=403,
                detail="Staff can only request time off.",
            )
        if not start_utc or not end_utc:
            raise HTTPException(
                status_code=400,
                detail="Time off requests require start and end time.",
            )
    request_id = str(uuid.uuid4())

    db.execute(
        """
        INSERT INTO schedule_change_requests
            (id, staff_id, requested_by, status, payload, reason)
        VALUES
            (:id, :staff_id, :requested_by, 'pending', :payload, :reason)
        """,
        {
            "id": request_id,
            "staff_id": staff_id,
            "requested_by": current_user.get("id"),
            "payload": payload.payload,
            "reason": payload.reason,
        },
    )

    if NOTIFICATIONS_ENABLED:
        admin_emails = db.execute(
            "SELECT email FROM users WHERE role IN ('admin', 'superadmin') AND is_active = TRUE"
        ).fetchall()
        for row in admin_emails:
            db.execute(
                """
                INSERT INTO notifications (id, booking_id, channel, type, recipient, status)
                VALUES (:id, NULL, 'email', 'schedule_change', :recipient, 'pending')
                """,
                {"id": str(uuid.uuid4()), "recipient": row[0]},
            )

    log_audit(
        db,
        current_user.get("id"),
        "create",
        "schedule_change_request",
        request_id,
        payload.model_dump(),
    )
    db.commit()

    created = db.execute(
        "SELECT * FROM schedule_change_requests WHERE id = :id",
        {"id": request_id},
    ).fetchone()
    return _normalize_schedule_request_row(created)

@router.get("/schedule-requests", response_model=List[ScheduleChangeRequestResponse])
async def list_schedule_change_requests(
    staff_id: Optional[str] = None,
    status: Optional[str] = None,
    current_user: dict = Depends(require_roles("staff", "admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """List schedule change requests (staff sees own, admin sees all)."""
    query = "SELECT * FROM schedule_change_requests WHERE 1=1"
    params: Dict[str, object] = {}

    if not is_admin(current_user):
        staff_id = current_user.get("id")
    elif staff_id in ("me", "", None):
        staff_id = current_user.get("id")

    if staff_id:
        query += " AND staff_id = :staff_id"
        params["staff_id"] = staff_id
    if status:
        query += " AND status = :status"
        params["status"] = status

    query += " ORDER BY created_at DESC"
    rows = db.execute(text(query), params).fetchall()
    return [_normalize_schedule_request_row(row) for row in rows]

@router.post("/schedule-requests/{request_id}/approve", response_model=ScheduleChangeRequestResponse)
async def approve_schedule_change_request(
    request_id: str,
    payload: ScheduleChangeRequestReview,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Approve a schedule change request (Admin only)."""
    request_row = db.execute(
        """
        SELECT * FROM schedule_change_requests
        WHERE id = :id AND status = 'pending'
        FOR UPDATE
        """,
        {"id": request_id},
    ).fetchone()

    if not request_row:
        raise HTTPException(status_code=404, detail="Request not found or already reviewed")

    request_map = request_row._mapping
    payload_data = request_map["payload"]
    if isinstance(payload_data, str):
        try:
            payload_data = json.loads(payload_data)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="Invalid request payload")

    if not isinstance(payload_data, dict):
        raise HTTPException(status_code=400, detail="Invalid request payload")

    target = payload_data.get("target")
    action = payload_data.get("action")

    if target == "weekly_schedule":
        schedule = db.execute(
            """
            SELECT id FROM staff_weekly_schedules
            WHERE staff_id = :staff_id
            ORDER BY is_default DESC, effective_from DESC NULLS LAST
            LIMIT 1
            """,
            {"staff_id": request_map["staff_id"]},
        ).fetchone()

        if not schedule:
            raise HTTPException(status_code=400, detail="Staff schedule not found")

        if action == "add":
            db.execute(
                """
                INSERT INTO staff_work_blocks (id, schedule_id, weekday, start_time_local, end_time_local)
                VALUES (:id, :schedule_id, :weekday, :start_time_local, :end_time_local)
                """,
                {
                    "id": str(uuid.uuid4()),
                    "schedule_id": schedule[0],
                    "weekday": payload_data.get("weekday"),
                    "start_time_local": payload_data.get("start_time"),
                    "end_time_local": payload_data.get("end_time"),
                },
            )
        elif action == "update":
            block_id = payload_data.get("block_id")
            if not block_id:
                raise HTTPException(status_code=400, detail="Missing block_id for update")
            db.execute(
                """
                UPDATE staff_work_blocks
                SET weekday = :weekday, start_time_local = :start_time_local, end_time_local = :end_time_local
                WHERE id = :id AND schedule_id = :schedule_id
                """,
                {
                    "id": block_id,
                    "schedule_id": schedule[0],
                    "weekday": payload_data.get("weekday"),
                    "start_time_local": payload_data.get("start_time"),
                    "end_time_local": payload_data.get("end_time"),
                },
            )
        elif action == "delete":
            block_id = payload_data.get("block_id")
            if not block_id:
                raise HTTPException(status_code=400, detail="Missing block_id for delete")
            db.execute(
                "DELETE FROM staff_work_blocks WHERE id = :id AND schedule_id = :schedule_id",
                {"id": block_id, "schedule_id": schedule[0]},
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid action for weekly schedule")

    elif target == "exception":
        if action == "add":
            db.execute(
                """
                INSERT INTO staff_exceptions
                    (id, staff_id, location_id, type, start_utc, end_utc, is_all_day, recurring_rule, reason, created_by)
                VALUES
                    (:id, :staff_id, NULL, :type, :start_utc, :end_utc, :is_all_day, NULL, :reason, :created_by)
                """,
                {
                    "id": str(uuid.uuid4()),
                    "staff_id": request_map["staff_id"],
                    "type": payload_data.get("type"),
                    "start_utc": payload_data.get("start_utc"),
                    "end_utc": payload_data.get("end_utc"),
                    "is_all_day": payload_data.get("is_all_day", False),
                    "reason": request_map.get("reason"),
                    "created_by": current_user.get("id"),
                },
            )
        elif action == "update":
            exception_id = payload_data.get("exception_id")
            if not exception_id:
                raise HTTPException(status_code=400, detail="Missing exception_id for update")
            db.execute(
                """
                UPDATE staff_exceptions
                SET type = :type, start_utc = :start_utc, end_utc = :end_utc,
                    is_all_day = :is_all_day, reason = :reason
                WHERE id = :id AND staff_id = :staff_id
                """,
                {
                    "id": exception_id,
                    "staff_id": request_map["staff_id"],
                    "type": payload_data.get("type"),
                    "start_utc": payload_data.get("start_utc"),
                    "end_utc": payload_data.get("end_utc"),
                    "is_all_day": payload_data.get("is_all_day", False),
                    "reason": request_map.get("reason"),
                },
            )
        elif action == "delete":
            exception_id = payload_data.get("exception_id")
            if not exception_id:
                raise HTTPException(status_code=400, detail="Missing exception_id for delete")
            db.execute(
                "DELETE FROM staff_exceptions WHERE id = :id AND staff_id = :staff_id",
                {"id": exception_id, "staff_id": request_map["staff_id"]},
            )
        else:
            raise HTTPException(status_code=400, detail="Invalid action for exception")
    else:
        raise HTTPException(status_code=400, detail="Invalid request target")

    result = db.execute(
        """
        UPDATE schedule_change_requests
        SET status = 'approved', review_note = :review_note,
            reviewed_by = :reviewed_by, reviewed_at = NOW()
        WHERE id = :id AND status = 'pending'
        RETURNING *
        """,
        {
            "id": request_id,
            "review_note": payload.review_note,
            "reviewed_by": current_user.get("id"),
        },
    ).fetchone()

    if NOTIFICATIONS_ENABLED:
        staff_email = db.execute(
            "SELECT email FROM users WHERE id = :id",
            {"id": result._mapping["staff_id"]},
        ).fetchone()
        if staff_email:
            db.execute(
                """
                INSERT INTO notifications (id, booking_id, channel, type, recipient, status)
                VALUES (:id, NULL, 'email', 'schedule_approved', :recipient, 'pending')
                """,
                {"id": str(uuid.uuid4()), "recipient": staff_email[0]},
            )

    log_audit(
        db,
        current_user.get("id"),
        "approve",
        "schedule_change_request",
        request_id,
        payload.model_dump(),
    )
    db.commit()

    return _normalize_uuid_values(dict(result._mapping))

@router.post("/schedule-requests/{request_id}/reject", response_model=ScheduleChangeRequestResponse)
async def reject_schedule_change_request(
    request_id: str,
    payload: ScheduleChangeRequestReview,
    current_user: dict = Depends(require_roles("admin", "superadmin")),
    db: Session = Depends(get_db),
):
    """Reject a schedule change request (Admin only)."""
    result = db.execute(
        """
        UPDATE schedule_change_requests
        SET status = 'rejected', review_note = :review_note,
            reviewed_by = :reviewed_by, reviewed_at = NOW()
        WHERE id = :id AND status = 'pending'
        RETURNING *
        """,
        {
            "id": request_id,
            "review_note": payload.review_note,
            "reviewed_by": current_user.get("id"),
        },
    ).fetchone()

    if not result:
        raise HTTPException(status_code=404, detail="Request not found or already reviewed")

    if NOTIFICATIONS_ENABLED:
        staff_email = db.execute(
            "SELECT email FROM users WHERE id = :id",
            {"id": result._mapping["staff_id"]},
        ).fetchone()
        if staff_email:
            db.execute(
                """
                INSERT INTO notifications (id, booking_id, channel, type, recipient, status)
                VALUES (:id, NULL, 'email', 'schedule_rejected', :recipient, 'pending')
                """,
                {"id": str(uuid.uuid4()), "recipient": staff_email[0]},
            )

    log_audit(
        db,
        current_user.get("id"),
        "reject",
        "schedule_change_request",
        request_id,
        payload.model_dump(),
    )
    db.commit()

    return _normalize_uuid_values(dict(result._mapping))
