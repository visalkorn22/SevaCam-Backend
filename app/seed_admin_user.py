import secrets
import string
import uuid

from passlib.context import CryptContext
from sqlalchemy import create_engine, text

from app.core.config import settings

ADMIN_EMAIL = "kornvisal222@gmail.com"
ADMIN_ROLE = "admin"
PASSWORD_LENGTH = 16

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def _secure_shuffle(chars: list[str]) -> None:
    for index in range(len(chars) - 1, 0, -1):
        swap_index = secrets.randbelow(index + 1)
        chars[index], chars[swap_index] = chars[swap_index], chars[index]


def generate_password(length: int = PASSWORD_LENGTH) -> str:
    if length < 12:
        raise ValueError("Password length must be at least 12")

    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    symbols = "!@#$%^&*()-_=+"
    all_chars = lowercase + uppercase + digits + symbols

    generated_chars = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(symbols),
    ]

    while len(generated_chars) < length:
        generated_chars.append(secrets.choice(all_chars))

    _secure_shuffle(generated_chars)
    return "".join(generated_chars)


def main() -> None:
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
    plain_password = generate_password()
    password_hash = pwd_context.hash(plain_password)

    with engine.begin() as conn:
        role_exists = conn.execute(
            text("SELECT 1 FROM roles WHERE name = :role"),
            {"role": ADMIN_ROLE},
        ).fetchone()
        if not role_exists:
            raise SystemExit("Role 'admin' not found. Run: python -m app.seed")

        existing_admin = conn.execute(
            text("SELECT id, email FROM users WHERE role = :role LIMIT 1"),
            {"role": ADMIN_ROLE},
        ).fetchone()
        if existing_admin and existing_admin.email != ADMIN_EMAIL:
            raise SystemExit(
                "An admin account already exists with a different email: "
                f"{existing_admin.email}. Update that user or remove it first."
            )

        existing_user = conn.execute(
            text("SELECT id FROM users WHERE email = :email"),
            {"email": ADMIN_EMAIL},
        ).fetchone()

        if existing_user:
            conn.execute(
                text(
                    """
                    UPDATE users
                    SET role = :role,
                        password_hash = :password_hash,
                        is_active = TRUE,
                        email_verified = TRUE
                    WHERE id = :id
                    """
                ),
                {
                    "id": existing_user.id,
                    "role": ADMIN_ROLE,
                    "password_hash": password_hash,
                },
            )
            action = "updated"
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO users (id, email, role, password_hash, is_active, email_verified)
                    VALUES (:id, :email, :role, :password_hash, TRUE, TRUE)
                    """
                ),
                {
                    "id": str(uuid.uuid4()),
                    "email": ADMIN_EMAIL,
                    "role": ADMIN_ROLE,
                    "password_hash": password_hash,
                },
            )
            action = "created"

    print(f"Admin user {action}: {ADMIN_EMAIL}")
    print("Generated password (save this now, it is not stored in plain text):")
    print(plain_password)


if __name__ == "__main__":
    main()
