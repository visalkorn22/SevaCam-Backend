import argparse

from sqlalchemy import create_engine, text

from app.core.config import settings

SEED_EMAILS = (
    "superadmin@example.com",
    "admin@example.com",
    "staff@example.com",
    "customer@example.com",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Remove legacy seeded users by email.",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Actually delete the users (required).",
    )
    return parser.parse_args()


def _placeholders() -> str:
    return ", ".join([f":e{index}" for index in range(1, len(SEED_EMAILS) + 1)])


def _params() -> dict:
    return {f"e{index}": email for index, email in enumerate(SEED_EMAILS, start=1)}


def main() -> None:
    args = _parse_args()
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    placeholders = _placeholders()
    params = _params()

    with engine.begin() as conn:
        rows = conn.execute(
            text(
                f"""
                SELECT id, email, role
                FROM users
                WHERE email IN ({placeholders})
                """
            ),
            params,
        ).fetchall()

        if not rows:
            print("No seeded users found.")
            return

        print("Seeded users found:")
        for row in rows:
            print(f"- {row.email} ({row.role})")

        if not args.confirm:
            print("Re-run with --confirm to delete these users.")
            return

        conn.execute(
            text(f"DELETE FROM users WHERE email IN ({placeholders})"),
            params,
        )

    print(f"Deleted {len(rows)} seeded user(s).")


if __name__ == "__main__":
    main()
