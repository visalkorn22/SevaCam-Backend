from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, text

from app.core.config import settings
from app.core.staff_profiles import calculate_experience_level, round_average_rating

DEFAULT_TIMEZONE = "Asia/Phnom_Penh"
SERVICE_ID = "6fe7f387-e42c-5a8b-991c-693683f9af52"
SERVICE_SCHEDULE_ID = "65b7dbe7-b21f-5b0d-a661-ab7df7a38c35"
SEED_NAMESPACE = uuid.UUID("38e84234-8ac9-4ae3-b3b8-50c57ac6fbf0")
SEED_DIR = Path(__file__).resolve().parent.parent / "uploads" / "seed"


@dataclass(frozen=True)
class StaffSeed:
    key: str
    full_name: str
    email: str
    phone: str
    avatar_fill: str
    avatar_accent: str
    skills: tuple[str, ...]
    bio: str
    completed_bookings: int
    review_ratings: tuple[int, ...]
    weekday_hours: tuple[int, int] = (9, 17)
    saturday_hours: tuple[int, int] = (10, 16)

    @property
    def avatar_path(self) -> str:
        return f"/uploads/seed/staff-{self.key}.svg"


def seed_uuid(name: str) -> str:
    return str(uuid.uuid5(SEED_NAMESPACE, name))


def build_ratings(*pairs: tuple[int, int]) -> tuple[int, ...]:
    ratings: list[int] = []
    for rating, count in pairs:
        ratings.extend([rating] * count)
    return tuple(ratings)


STAFF_SEEDS = (
    StaffSeed(
        key="dara",
        full_name="Mr. Dara",
        email="sample.staff.dara@example.com",
        phone="+855 12 401 201",
        avatar_fill="#1f4b99",
        avatar_accent="#f4bf75",
        skills=(
            "Portrait Photography",
            "Wedding Photography",
            "Photo Editing",
        ),
        bio=(
            "Specializes in portrait and event sessions with polished Lightroom "
            "editing and calm on-site direction."
        ),
        completed_bookings=58,
        review_ratings=build_ratings((5, 50), (4, 4)),
    ),
    StaffSeed(
        key="sophea",
        full_name="Ms. Sophea",
        email="sample.staff.sophea@example.com",
        phone="+855 10 882 614",
        avatar_fill="#4c8a4b",
        avatar_accent="#f8df98",
        skills=(
            "Portrait Photography",
            "Product Photography",
            "Photo Retouching",
        ),
        bio=(
            "Focused on clean lifestyle and product photography with a strong eye "
            "for color balance and commercial framing."
        ),
        completed_bookings=42,
        review_ratings=build_ratings((5, 24), (4, 11)),
    ),
    StaffSeed(
        key="vannak",
        full_name="Mr. Vannak",
        email="sample.staff.vannak@example.com",
        phone="+855 15 330 918",
        avatar_fill="#7a4a9e",
        avatar_accent="#f7c7d9",
        skills=(
            "Event Photography",
            "Couple Photoshoot",
            "Basic Photo Editing",
        ),
        bio=(
            "Handles outdoor and event sessions confidently, with reliable posing "
            "guidance and fast basic post-processing."
        ),
        completed_bookings=24,
        review_ratings=build_ratings((5, 2), (4, 16)),
    ),
    StaffSeed(
        key="lina",
        full_name="Ms. Lina",
        email="sample.staff.lina@example.com",
        phone="+855 96 731 442",
        avatar_fill="#bf6b52",
        avatar_accent="#f2d2a2",
        skills=(
            "Portrait Photography",
            "Studio Assistance",
            "Photo Selection Support",
        ),
        bio=(
            "A newer team member who is growing in guided portrait sessions and "
            "customer support during studio bookings."
        ),
        completed_bookings=16,
        review_ratings=build_ratings((4, 3), (3, 8), (2, 1)),
    ),
)

FIRST_NAMES = (
    "Sokha",
    "Rina",
    "Pisey",
    "Malis",
    "Vicheka",
    "Kosal",
    "Rathanak",
    "Sreypov",
    "Chanthy",
    "Dalin",
    "Sovann",
    "Mony",
)
LAST_NAMES = (
    "Ly",
    "Chan",
    "Kim",
    "Sok",
    "Chea",
    "Ngoun",
    "Heng",
    "Long",
    "Nim",
    "Touch",
)
REVIEW_COMMENTS = {
    5: (
        "Excellent direction and very easy to work with.",
        "The final photos looked polished and professional.",
        "Strong communication before and during the session.",
        "Very happy with the posing guidance and editing.",
    ),
    4: (
        "Good session overall and the photos turned out well.",
        "Helpful guidance and solid final delivery.",
        "Friendly service with good photo quality.",
        "Would book again for a similar session.",
    ),
    3: (
        "The session was okay, but I wanted a little more guidance.",
        "Results were acceptable, though a few shots felt rushed.",
        "Decent experience overall with room to improve.",
    ),
    2: (
        "The session felt a bit unorganized for my preference.",
        "Communication could have been clearer during the shoot.",
    ),
}


def build_customer_identity(index: int) -> tuple[str, str]:
    first = FIRST_NAMES[index % len(FIRST_NAMES)]
    last = LAST_NAMES[(index // len(FIRST_NAMES)) % len(LAST_NAMES)]
    full_name = f"{first} {last}"
    email = f"sample.customer.{index:03d}@example.com"
    return full_name, email


def build_review_comment(staff_name: str, rating: int, index: int) -> str:
    templates = REVIEW_COMMENTS.get(rating) or REVIEW_COMMENTS[4]
    template = templates[index % len(templates)]
    return f"{template} Session with {staff_name}."


def ensure_seed_artifacts() -> None:
    SEED_DIR.mkdir(parents=True, exist_ok=True)
    for staff in STAFF_SEEDS:
        initials = "".join(part[0] for part in staff.full_name.split() if part[:1]).upper()[:2]
        svg = f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 240" role="img" aria-label="{staff.full_name}">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="{staff.avatar_fill}" />
      <stop offset="100%" stop-color="{staff.avatar_accent}" />
    </linearGradient>
  </defs>
  <rect width="240" height="240" rx="48" fill="url(#bg)" />
  <circle cx="120" cy="96" r="42" fill="rgba(255,255,255,0.18)" />
  <path d="M52 208c16-35 43-52 68-52s52 17 68 52" fill="rgba(255,255,255,0.18)" />
  <text x="120" y="214" text-anchor="middle" fill="#fff8eb" font-size="62" font-family="Georgia, serif" font-weight="700">{initials}</text>
</svg>
"""
        (SEED_DIR / f"staff-{staff.key}.svg").write_text(svg, encoding="utf-8")


def upsert_user(conn, staff: StaffSeed) -> str:
    return str(
        conn.execute(
            text(
                """
                INSERT INTO users (
                    id, email, full_name, role, phone, avatar_url, timezone,
                    password_hash, is_active
                )
                VALUES (
                    :id, :email, :full_name, 'staff', :phone, :avatar_url, :timezone,
                    :password_hash, TRUE
                )
                ON CONFLICT (id)
                DO UPDATE SET
                    email = EXCLUDED.email,
                    full_name = EXCLUDED.full_name,
                    role = EXCLUDED.role,
                    phone = EXCLUDED.phone,
                    avatar_url = EXCLUDED.avatar_url,
                    timezone = EXCLUDED.timezone,
                    password_hash = EXCLUDED.password_hash,
                    is_active = TRUE
                RETURNING id
                """
            ),
            {
                "id": seed_uuid(f"user:{staff.key}"),
                "email": staff.email,
                "full_name": staff.full_name,
                "phone": staff.phone,
                "avatar_url": staff.avatar_path,
                "timezone": DEFAULT_TIMEZONE,
                "password_hash": None,
            },
        ).scalar_one()
    )


def resolve_service_admin_id(conn) -> str | None:
    row = conn.execute(
        text(
            """
            SELECT id
            FROM users
            WHERE role IN ('superadmin', 'admin')
              AND is_active = TRUE
            ORDER BY CASE WHEN role = 'superadmin' THEN 0 ELSE 1 END, created_at
            LIMIT 1
            """
        )
    ).fetchone()
    return str(row.id) if row else None


def upsert_service(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO services (
                id, admin_id, name, public_name, internal_name, category, tags,
                description, inclusions, prep_notes, duration_minutes, price,
                deposit_amount, buffer_minutes, max_capacity, is_active, image_url,
                image_urls, is_archived, paused_from, paused_until
            )
            VALUES (
                :id, :admin_id, :name, :public_name, :internal_name, :category, :tags,
                :description, :inclusions, :prep_notes, :duration_minutes, :price,
                :deposit_amount, :buffer_minutes, :max_capacity, TRUE, NULL,
                NULL, FALSE, NULL, NULL
            )
            ON CONFLICT (id)
            DO UPDATE SET
                admin_id = EXCLUDED.admin_id,
                name = EXCLUDED.name,
                public_name = EXCLUDED.public_name,
                internal_name = EXCLUDED.internal_name,
                category = EXCLUDED.category,
                tags = EXCLUDED.tags,
                description = EXCLUDED.description,
                inclusions = EXCLUDED.inclusions,
                prep_notes = EXCLUDED.prep_notes,
                duration_minutes = EXCLUDED.duration_minutes,
                price = EXCLUDED.price,
                deposit_amount = EXCLUDED.deposit_amount,
                buffer_minutes = EXCLUDED.buffer_minutes,
                max_capacity = EXCLUDED.max_capacity,
                is_active = TRUE,
                is_archived = FALSE,
                paused_from = NULL,
                paused_until = NULL
            """
        ),
        {
            "id": SERVICE_ID,
            "admin_id": resolve_service_admin_id(conn),
            "name": "Photoshoot",
            "public_name": "Studio & Event Photoshoot",
            "internal_name": "Seeded sample photoshoot service",
            "category": "Photoshoot",
            "tags": ["portrait", "event", "product", "editing"],
            "description": (
                "Sample photoshoot service with seeded staff profiles, booking history, "
                "and customer reviews so the experience cards are visible immediately."
            ),
            "inclusions": "\n".join(
                (
                    "90-minute guided session",
                    "Basic retouching on selected photos",
                    "Digital delivery gallery",
                )
            ),
            "prep_notes": (
                "Bring one or two outfit options and arrive 10 minutes early for "
                "lighting and framing setup."
            ),
            "duration_minutes": 90,
            "price": Decimal("120.00"),
            "deposit_amount": Decimal("30.00"),
            "buffer_minutes": 15,
            "max_capacity": 1,
        },
    )


def upsert_service_schedule(conn) -> None:
    conn.execute(
        text(
            """
            INSERT INTO service_operating_schedules (
                id, service_id, timezone, rule_type, open_time, close_time,
                effective_from, effective_to, is_active
            )
            VALUES (
                :id, :service_id, :timezone, 'daily', :open_time, :close_time,
                NULL, NULL, TRUE
            )
            ON CONFLICT (id)
            DO UPDATE SET
                service_id = EXCLUDED.service_id,
                timezone = EXCLUDED.timezone,
                rule_type = EXCLUDED.rule_type,
                open_time = EXCLUDED.open_time,
                close_time = EXCLUDED.close_time,
                effective_from = NULL,
                effective_to = NULL,
                is_active = TRUE
            """
        ),
        {
            "id": SERVICE_SCHEDULE_ID,
            "service_id": SERVICE_ID,
            "timezone": DEFAULT_TIMEZONE,
            "open_time": time(8, 0),
            "close_time": time(19, 0),
        },
    )


def upsert_staff_assignment(conn, staff_id: str, staff: StaffSeed) -> None:
    conn.execute(
        text(
            """
            INSERT INTO staff_services (
                id, staff_id, service_id, price_override, deposit_override,
                duration_override, buffer_override, capacity_override,
                is_bookable, is_temporarily_unavailable, admin_only, skills, bio
            )
            VALUES (
                :id, :staff_id, :service_id, NULL, NULL,
                NULL, NULL, NULL, TRUE, FALSE, FALSE, :skills, :bio
            )
            ON CONFLICT (staff_id, service_id)
            DO UPDATE SET
                is_bookable = TRUE,
                is_temporarily_unavailable = FALSE,
                admin_only = FALSE,
                skills = EXCLUDED.skills,
                bio = EXCLUDED.bio
            """
        ),
        {
            "id": seed_uuid(f"staff-service:{staff.key}"),
            "staff_id": staff_id,
            "service_id": SERVICE_ID,
            "skills": list(staff.skills),
            "bio": staff.bio,
        },
    )


def upsert_staff_schedule(conn, staff_id: str, staff: StaffSeed) -> str:
    schedule_id = seed_uuid(f"staff-schedule:{staff.key}")
    conn.execute(
        text(
            """
            INSERT INTO staff_weekly_schedules (
                id, staff_id, timezone, effective_from, effective_to,
                is_default, location_id
            )
            VALUES (
                :id, :staff_id, :timezone, :effective_from, NULL, TRUE, NULL
            )
            ON CONFLICT (id)
            DO UPDATE SET
                staff_id = EXCLUDED.staff_id,
                timezone = EXCLUDED.timezone,
                effective_from = EXCLUDED.effective_from,
                effective_to = NULL,
                is_default = TRUE,
                location_id = NULL
            """
        ),
        {
            "id": schedule_id,
            "staff_id": staff_id,
            "timezone": DEFAULT_TIMEZONE,
            "effective_from": date.today() - timedelta(days=365),
        },
    )
    return schedule_id


def upsert_time_block(
    conn,
    *,
    table_name: str,
    block_id: str,
    schedule_id: str,
    weekday: int,
    start_time_local: time,
    end_time_local: time,
) -> None:
    conn.execute(
        text(
            f"""
            INSERT INTO {table_name} (
                id, schedule_id, weekday, start_time_local, end_time_local
            )
            VALUES (
                :id, :schedule_id, :weekday, :start_time_local, :end_time_local
            )
            ON CONFLICT (id)
            DO UPDATE SET
                schedule_id = EXCLUDED.schedule_id,
                weekday = EXCLUDED.weekday,
                start_time_local = EXCLUDED.start_time_local,
                end_time_local = EXCLUDED.end_time_local
            """
        ),
        {
            "id": block_id,
            "schedule_id": schedule_id,
            "weekday": weekday,
            "start_time_local": start_time_local,
            "end_time_local": end_time_local,
        },
    )


def upsert_staff_availability(conn, schedule_id: str, staff: StaffSeed) -> None:
    weekday_start, weekday_end = staff.weekday_hours
    saturday_start, saturday_end = staff.saturday_hours

    for weekday in (1, 2, 3, 4, 5):
        upsert_time_block(
            conn,
            table_name="staff_work_blocks",
            block_id=seed_uuid(f"staff-work:{staff.key}:{weekday}"),
            schedule_id=schedule_id,
            weekday=weekday,
            start_time_local=time(weekday_start, 0),
            end_time_local=time(weekday_end, 0),
        )
        upsert_time_block(
            conn,
            table_name="staff_break_blocks",
            block_id=seed_uuid(f"staff-break:{staff.key}:{weekday}"),
            schedule_id=schedule_id,
            weekday=weekday,
            start_time_local=time(12, 0),
            end_time_local=time(13, 0),
        )

    upsert_time_block(
        conn,
        table_name="staff_work_blocks",
        block_id=seed_uuid(f"staff-work:{staff.key}:6"),
        schedule_id=schedule_id,
        weekday=6,
        start_time_local=time(saturday_start, 0),
        end_time_local=time(saturday_end, 0),
    )
    upsert_time_block(
        conn,
        table_name="staff_break_blocks",
        block_id=seed_uuid(f"staff-break:{staff.key}:6"),
        schedule_id=schedule_id,
        weekday=6,
        start_time_local=time(12, 30),
        end_time_local=time(13, 15),
    )


def upsert_customer(conn, customer_index: int) -> str:
    customer_id = seed_uuid(f"customer:{customer_index}")
    full_name, email = build_customer_identity(customer_index)
    conn.execute(
        text(
            """
            INSERT INTO customers (
                id, user_id, full_name, email, phone, timezone, notes
            )
            VALUES (
                :id, NULL, :full_name, :email, NULL, :timezone, :notes
            )
            ON CONFLICT (id)
            DO UPDATE SET
                full_name = EXCLUDED.full_name,
                email = EXCLUDED.email,
                timezone = EXCLUDED.timezone,
                notes = EXCLUDED.notes
            """
        ),
        {
            "id": customer_id,
            "full_name": full_name,
            "email": email,
            "timezone": DEFAULT_TIMEZONE,
            "notes": "Seeded sample customer for staff profile previews.",
        },
    )
    return customer_id


def upsert_booking(
    conn,
    *,
    booking_id: str,
    staff_id: str,
    customer_id: str,
    starts_at_utc: datetime,
    duration_minutes: int,
) -> None:
    ends_at_utc = starts_at_utc + timedelta(minutes=duration_minutes)
    conn.execute(
        text(
            """
            INSERT INTO bookings (
                id, service_id, staff_id, customer_id, start_time_utc, end_time_utc,
                status, payment_status, booking_source, customer_timezone, created_at
            )
            VALUES (
                :id, :service_id, :staff_id, :customer_id, :start_time_utc, :end_time_utc,
                'completed', 'paid', 'web', :customer_timezone, :created_at
            )
            ON CONFLICT (id)
            DO UPDATE SET
                service_id = EXCLUDED.service_id,
                staff_id = EXCLUDED.staff_id,
                customer_id = EXCLUDED.customer_id,
                start_time_utc = EXCLUDED.start_time_utc,
                end_time_utc = EXCLUDED.end_time_utc,
                status = 'completed',
                payment_status = 'paid',
                booking_source = 'web',
                customer_timezone = EXCLUDED.customer_timezone,
                created_at = EXCLUDED.created_at
            """
        ),
        {
            "id": booking_id,
            "service_id": SERVICE_ID,
            "staff_id": staff_id,
            "customer_id": customer_id,
            "start_time_utc": starts_at_utc,
            "end_time_utc": ends_at_utc,
            "customer_timezone": DEFAULT_TIMEZONE,
            "created_at": starts_at_utc - timedelta(days=7),
        },
    )


def upsert_review(
    conn,
    *,
    review_id: str,
    booking_id: str,
    rating: int,
    comment: str,
    created_at: datetime,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO reviews (
                id, booking_id, rating, comment, is_approved, created_at
            )
            VALUES (
                :id, :booking_id, :rating, :comment, TRUE, :created_at
            )
            ON CONFLICT (id)
            DO UPDATE SET
                booking_id = EXCLUDED.booking_id,
                rating = EXCLUDED.rating,
                comment = EXCLUDED.comment,
                is_approved = TRUE,
                created_at = EXCLUDED.created_at
            """
        ),
        {
            "id": review_id,
            "booking_id": booking_id,
            "rating": rating,
            "comment": comment,
            "created_at": created_at,
        },
    )


def seed_history(conn, staff_id: str, staff: StaffSeed, customer_start_index: int) -> int:
    review_count = len(staff.review_ratings)
    duration_minutes = 90

    for index in range(staff.completed_bookings):
        customer_index = customer_start_index + index
        customer_id = upsert_customer(conn, customer_index)
        booking_id = seed_uuid(f"booking:{staff.key}:{index}")
        days_ago = 3 + index
        slot_hour = (9, 11, 14, 16)[index % 4]
        starts_at_utc = datetime.now(UTC).replace(
            hour=slot_hour,
            minute=0,
            second=0,
            microsecond=0,
            tzinfo=None,
        ) - timedelta(days=days_ago)

        upsert_booking(
            conn,
            booking_id=booking_id,
            staff_id=staff_id,
            customer_id=customer_id,
            starts_at_utc=starts_at_utc,
            duration_minutes=duration_minutes,
        )

        if index < review_count:
            rating = staff.review_ratings[index]
            upsert_review(
                conn,
                review_id=seed_uuid(f"review:{staff.key}:{index}"),
                booking_id=booking_id,
                rating=rating,
                comment=build_review_comment(staff.full_name, rating, index),
                created_at=starts_at_utc + timedelta(days=1),
            )

    return customer_start_index + staff.completed_bookings


def print_summary() -> None:
    print("Seeded sample service: Studio & Event Photoshoot")
    print(f"Service ID: {SERVICE_ID}")
    print("Sample staff accounts were created for preview only and do not include login passwords.")
    for staff in STAFF_SEEDS:
        raw_average = (
            sum(staff.review_ratings) / len(staff.review_ratings)
            if staff.review_ratings
            else None
        )
        rounded_average = round_average_rating(raw_average)
        experience = calculate_experience_level(
            rounded_average,
            staff.completed_bookings,
        )
        print(
            "- "
            f"{staff.full_name}: {rounded_average or 'New'} rating, "
            f"{len(staff.review_ratings)} reviews, "
            f"{staff.completed_bookings} completed bookings, "
            f"{experience}"
        )


def main() -> None:
    ensure_seed_artifacts()
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

    with engine.begin() as conn:
        upsert_service(conn)
        upsert_service_schedule(conn)

        customer_index = 1
        for staff in STAFF_SEEDS:
            staff_id = upsert_user(conn, staff)
            upsert_staff_assignment(conn, staff_id, staff)
            schedule_id = upsert_staff_schedule(conn, staff_id, staff)
            upsert_staff_availability(conn, schedule_id, staff)
            customer_index = seed_history(conn, staff_id, staff, customer_index)

    print_summary()


if __name__ == "__main__":
    main()
