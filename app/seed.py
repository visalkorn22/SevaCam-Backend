from sqlalchemy import create_engine, text

from app.core.config import settings

ROLE_DEFINITIONS = [
    {"name": "customer", "description": "Default customer role", "is_unique": False},
    {"name": "staff", "description": "Staff member role", "is_unique": False},
    {"name": "admin", "description": "Administrator role", "is_unique": True},
    {"name": "superadmin", "description": "Super administrator role", "is_unique": True},
]

PERMISSIONS = {
    "services:read": "View services",
    "services:manage": "Create, update, and delete services",
    "staff:manage": "Assign and manage staff",
    "availability:manage_own": "Manage own availability",
    "bookings:create": "Create bookings",
    "bookings:read_own": "View own bookings",
    "bookings:read_assigned": "View assigned bookings",
    "bookings:manage": "Manage all bookings",
    "payments:read_own": "View own payments",
    "payments:manage": "Manage all payments",
    "customers:read_own": "View own customer profile",
    "customers:manage": "Manage all customers",
    "reviews:create": "Create reviews",
    "reviews:manage": "Moderate reviews",
    "analytics:read": "View analytics",
    "roles:assign": "Assign and change user roles",
    "roles:promote_staff": "Promote customer to staff",
    "roles:promote_admin": "Promote staff to admin",
    "roles:promote_superadmin": "Promote admin to superadmin",
}

ROLE_PERMISSIONS = {
    "customer": [
        "services:read",
        "bookings:create",
        "bookings:read_own",
        "payments:read_own",
        "customers:read_own",
        "reviews:create",
    ],
    "staff": [
        "services:read",
        "availability:manage_own",
        "bookings:read_assigned",
    ],
    "admin": [
        "services:manage",
        "staff:manage",
        "bookings:manage",
        "payments:manage",
        "customers:manage",
        "reviews:manage",
        "analytics:read",
        "roles:assign",
        "roles:promote_staff",
    ],
    "superadmin": [
        "services:manage",
        "staff:manage",
        "bookings:manage",
        "payments:manage",
        "customers:manage",
        "reviews:manage",
        "analytics:read",
        "roles:assign",
        "roles:promote_staff",
        "roles:promote_admin",
        "roles:promote_superadmin",
    ],
}


def _upsert_roles(conn) -> None:
    for role in ROLE_DEFINITIONS:
        conn.execute(
            text(
                """
                INSERT INTO roles (name, description, is_unique)
                VALUES (:name, :description, :is_unique)
                ON CONFLICT (name)
                DO UPDATE SET description = EXCLUDED.description,
                              is_unique = EXCLUDED.is_unique
                """
            ),
            role,
        )


def _upsert_permissions(conn) -> None:
    for code, description in PERMISSIONS.items():
        conn.execute(
            text(
                """
                INSERT INTO permissions (code, description)
                VALUES (:code, :description)
                ON CONFLICT (code)
                DO UPDATE SET description = EXCLUDED.description
                """
            ),
            {"code": code, "description": description},
        )


def _assign_role_permissions(conn) -> None:
    for role_name, permission_codes in ROLE_PERMISSIONS.items():
        for code in permission_codes:
            conn.execute(
                text(
                    """
                    INSERT INTO role_permissions (role_name, permission_code)
                    VALUES (:role_name, :permission_code)
                    ON CONFLICT (role_name, permission_code) DO NOTHING
                    """
                ),
                {"role_name": role_name, "permission_code": code},
            )


def main() -> None:
    engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)

    with engine.begin() as conn:
        _upsert_roles(conn)
        _upsert_permissions(conn)
        _assign_role_permissions(conn)


if __name__ == "__main__":
    main()
