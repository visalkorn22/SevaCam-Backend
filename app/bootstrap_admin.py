import argparse
import uuid

from passlib.context import CryptContext
from sqlalchemy import create_engine, text

from app.core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create or update an initial admin/superadmin user.",
    )
    parser.add_argument("--email", required=True, help="User email address.")
    parser.add_argument("--password", required=True, help="User password (min 6 chars).")
    parser.add_argument("--full-name", default=None, help="Optional full name.")
    parser.add_argument(
        "--role",
        default="superadmin",
        choices=["admin", "superadmin", "staff", "customer"],
        help="Role to assign.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()

    if len(args.password) < 6:
        raise SystemExit("Password must be at least 6 characters.")

    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

    with engine.begin() as conn:
        role_row = conn.execute(
            text("SELECT name, is_unique FROM roles WHERE name = :role"),
            {"role": args.role},
        ).fetchone()
        if not role_row:
            raise SystemExit(
                f"Role '{args.role}' does not exist. Run the seed to create roles first.",
            )

        if role_row.is_unique:
            existing_unique = conn.execute(
                text("SELECT id, email FROM users WHERE role = :role"),
                {"role": args.role},
            ).fetchone()
            if existing_unique and existing_unique.email != args.email:
                raise SystemExit(
                    f"Role '{args.role}' is already assigned to {existing_unique.email}.",
                )

        existing_user = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": args.email},
        ).fetchone()

        password_hash = pwd_context.hash(args.password)

        if existing_user:
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET role = :role,
                        password_hash = :password_hash,
                        full_name = COALESCE(:full_name, full_name),
                        is_active = TRUE
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing_user.id,
                    "role": args.role,
                    "password_hash": password_hash,
                    "full_name": args.full_name,
                },
            )
            user_id = existing_user.id
            action = "updated"
        else:
            user_id = str(uuid.uuid4())
            conn.execute(
                text(
                    """
                    INSERT INTO users (id, email, full_name, role, password_hash, is_active)
                    VALUES (:id, :email, :full_name, :role, :password_hash, TRUE)
                    """
                ),
                {
                    "id": user_id,
                    "email": args.email,
                    "full_name": args.full_name,
                    "role": args.role,
                    "password_hash": password_hash,
                },
            )
            action = "created"

    print(f"Bootstrap user {action}: {args.email} (role={args.role}, id={user_id})")


if __name__ == "__main__":
    main()
