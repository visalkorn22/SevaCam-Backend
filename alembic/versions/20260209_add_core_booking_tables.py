"""add core booking tables

Revision ID: 20260209addcore
Revises: 20260116addpw
Create Date: 2026-02-09 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260209addcore"
down_revision: Union[str, Sequence[str], None] = "20260116addpw"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_fk(inspector, table: str, columns: list[str], referred_table: str) -> bool:
    for fk in inspector.get_foreign_keys(table):
        if fk.get("referred_table") == referred_table and fk.get("constrained_columns") == columns:
            return True
    return False


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "roles" not in table_names:
        op.create_table(
            "roles",
            sa.Column("name", sa.String(length=50), primary_key=True, nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column(
                "is_unique",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    if "permissions" not in table_names:
        op.create_table(
            "permissions",
            sa.Column("code", sa.String(length=100), primary_key=True, nullable=False),
            sa.Column("description", sa.String(length=255), nullable=True),
        )

    if "role_permissions" not in table_names:
        op.create_table(
            "role_permissions",
            sa.Column("role_name", sa.String(length=50), nullable=False),
            sa.Column("permission_code", sa.String(length=100), nullable=False),
            sa.ForeignKeyConstraint(["role_name"], ["roles.name"]),
            sa.ForeignKeyConstraint(["permission_code"], ["permissions.code"]),
            sa.PrimaryKeyConstraint("role_name", "permission_code"),
        )

    locations_created = False
    if "locations" not in table_names:
        op.create_table(
            "locations",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("timezone", sa.String(length=50), nullable=False),
            sa.Column("address", sa.String(length=255), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        locations_created = True

    if "users" in table_names:
        user_columns = {col["name"] for col in inspector.get_columns("users")}
        if "location_id" not in user_columns:
            op.add_column("users", sa.Column("location_id", sa.UUID(), nullable=True))
        if ("locations" in table_names or locations_created) and not _has_fk(
            inspector, "users", ["location_id"], "locations"
        ):
            op.create_foreign_key(
                "fk_users_location_id_locations",
                "users",
                "locations",
                ["location_id"],
                ["id"],
            )

    if "user_profiles" not in table_names:
        op.create_table(
            "user_profiles",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("full_name", sa.String(length=150), nullable=True),
            sa.Column("phone", sa.String(length=50), nullable=True),
            sa.Column("avatar_url", sa.String(length=255), nullable=True),
            sa.Column("timezone", sa.String(length=50), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
            sa.UniqueConstraint("user_id", name="uq_user_profiles_user_id"),
        )

    if "services" not in table_names:
        op.create_table(
            "services",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("admin_id", sa.UUID(), nullable=True),
            sa.Column("name", sa.String(length=150), nullable=False),
            sa.Column("public_name", sa.String(length=150), nullable=True),
            sa.Column("internal_name", sa.String(length=150), nullable=True),
            sa.Column("category", sa.String(length=100), nullable=True),
            sa.Column("tags", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("inclusions", sa.Text(), nullable=True),
            sa.Column("prep_notes", sa.Text(), nullable=True),
            sa.Column(
                "duration_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("60"),
            ),
            sa.Column("price", sa.Numeric(10, 2), nullable=False),
            sa.Column(
                "deposit_amount",
                sa.Numeric(10, 2),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "buffer_minutes",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
            sa.Column(
                "max_capacity",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column("image_url", sa.String(length=255), nullable=True),
            sa.Column("image_urls", postgresql.ARRAY(sa.String()), nullable=True),
            sa.Column(
                "is_archived",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            sa.Column("paused_from", sa.DateTime(), nullable=True),
            sa.Column("paused_until", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["admin_id"], ["users.id"]),
        )

    if "staff_services" not in table_names:
        op.create_table(
            "staff_services",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("price_override", sa.Numeric(10, 2), nullable=True),
            sa.Column("deposit_override", sa.Numeric(10, 2), nullable=True),
            sa.Column("duration_override", sa.Integer(), nullable=True),
            sa.Column("buffer_override", sa.Integer(), nullable=True),
            sa.Column("capacity_override", sa.Integer(), nullable=True),
            sa.Column(
                "is_bookable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "is_temporarily_unavailable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "admin_only",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
            sa.UniqueConstraint("staff_id", "service_id", name="uq_staff_services_staff_service"),
        )

    if "staff_service_overrides" not in table_names:
        op.create_table(
            "staff_service_overrides",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("price_override", sa.Numeric(10, 2), nullable=True),
            sa.Column("deposit_override", sa.Numeric(10, 2), nullable=True),
            sa.Column("duration_override", sa.Integer(), nullable=True),
            sa.Column("buffer_override", sa.Integer(), nullable=True),
            sa.Column("capacity_override", sa.Integer(), nullable=True),
            sa.Column(
                "is_bookable",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
            sa.UniqueConstraint("staff_id", "service_id", name="uq_staff_service_overrides_staff_service"),
        )

    if "service_operating_schedules" not in table_names:
        op.create_table(
            "service_operating_schedules",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("timezone", sa.String(length=50), nullable=False),
            sa.Column("rule_type", sa.String(length=20), nullable=False),
            sa.Column("open_time", sa.Time(), nullable=True),
            sa.Column("close_time", sa.Time(), nullable=True),
            sa.Column("effective_from", sa.Date(), nullable=True),
            sa.Column("effective_to", sa.Date(), nullable=True),
            sa.Column(
                "is_active",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        )

    if "service_operating_rules" not in table_names:
        op.create_table(
            "service_operating_rules",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.UUID(), nullable=False),
            sa.Column("rule_type", sa.String(length=30), nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=True),
            sa.Column("month_day", sa.Integer(), nullable=True),
            sa.Column("nth", sa.Integer(), nullable=True),
            sa.Column("start_time", sa.Time(), nullable=True),
            sa.Column("end_time", sa.Time(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["schedule_id"], ["service_operating_schedules.id"]),
        )

    if "service_operating_exceptions" not in table_names:
        op.create_table(
            "service_operating_exceptions",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column(
                "is_open",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("start_time", sa.Time(), nullable=True),
            sa.Column("end_time", sa.Time(), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        )

    if "availability_rules" not in table_names:
        op.create_table(
            "availability_rules",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=True),
            sa.Column("day_of_week", sa.Integer(), nullable=False),
            sa.Column("start_time", sa.Time(), nullable=False),
            sa.Column("end_time", sa.Time(), nullable=False),
            sa.Column("timezone", sa.String(length=50), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        )

    if "availability_exceptions" not in table_names:
        op.create_table(
            "availability_exceptions",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=True),
            sa.Column("date", sa.Date(), nullable=False),
            sa.Column(
                "is_available",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("start_time", sa.Time(), nullable=True),
            sa.Column("end_time", sa.Time(), nullable=True),
            sa.Column("reason", sa.String(length=255), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
        )

    if "customers" not in table_names:
        op.create_table(
            "customers",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=True),
            sa.Column("full_name", sa.String(length=150), nullable=False),
            sa.Column("email", sa.String(length=150), nullable=False),
            sa.Column("phone", sa.String(length=50), nullable=True),
            sa.Column("timezone", sa.String(length=50), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column(
                "is_blocked",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        )

    if "bookings" not in table_names:
        op.create_table(
            "bookings",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("customer_id", sa.UUID(), nullable=False),
            sa.Column("start_time_utc", sa.DateTime(), nullable=False),
            sa.Column("end_time_utc", sa.DateTime(), nullable=False),
            sa.Column(
                "status",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "payment_status",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "booking_source",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'web'"),
            ),
            sa.Column(
                "customer_timezone",
                sa.String(length=50),
                nullable=False,
                server_default=sa.text("'UTC'"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        )

    if "booking_changes" not in table_names:
        op.create_table(
            "booking_changes",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("booking_id", sa.UUID(), nullable=False),
            sa.Column("old_start_time", sa.DateTime(), nullable=True),
            sa.Column("new_start_time", sa.DateTime(), nullable=True),
            sa.Column("change_type", sa.String(length=30), nullable=False),
            sa.Column("changed_by", sa.UUID(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
            sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        )

    if "booking_logs" not in table_names:
        op.create_table(
            "booking_logs",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("booking_id", sa.UUID(), nullable=False),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("performed_by", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
            sa.ForeignKeyConstraint(["performed_by"], ["users.id"]),
        )

    if "payments" not in table_names:
        op.create_table(
            "payments",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("booking_id", sa.UUID(), nullable=False),
            sa.Column("provider", sa.String(length=50), nullable=False),
            sa.Column("provider_reference", sa.String(length=255), nullable=True),
            sa.Column("amount", sa.Numeric(10, 2), nullable=False),
            sa.Column(
                "currency",
                sa.String(length=10),
                nullable=False,
                server_default=sa.text("'USD'"),
            ),
            sa.Column(
                "status",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        )

    if "refunds" not in table_names:
        op.create_table(
            "refunds",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("payment_id", sa.UUID(), nullable=False),
            sa.Column("amount", sa.Numeric(10, 2), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("provider_refund_id", sa.String(length=255), nullable=True),
            sa.Column(
                "status",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'pending'"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["payment_id"], ["payments.id"]),
        )

    if "notifications" not in table_names:
        op.create_table(
            "notifications",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("booking_id", sa.UUID(), nullable=True),
            sa.Column("channel", sa.String(length=20), nullable=False),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("recipient", sa.String(length=255), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("sent_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        )

    if "waitlist" not in table_names:
        op.create_table(
            "waitlist",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("customer_id", sa.UUID(), nullable=False),
            sa.Column("preferred_date", sa.Date(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
            sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        )

    if "reviews" not in table_names:
        op.create_table(
            "reviews",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("booking_id", sa.UUID(), nullable=False),
            sa.Column("rating", sa.Integer(), nullable=False),
            sa.Column("comment", sa.Text(), nullable=True),
            sa.Column(
                "is_approved",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["booking_id"], ["bookings.id"]),
        )

    if "staff_weekly_schedules" not in table_names:
        op.create_table(
            "staff_weekly_schedules",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("timezone", sa.String(length=50), nullable=False),
            sa.Column("effective_from", sa.Date(), nullable=True),
            sa.Column("effective_to", sa.Date(), nullable=True),
            sa.Column(
                "is_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("location_id", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
        )

    if "staff_work_blocks" not in table_names:
        op.create_table(
            "staff_work_blocks",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.UUID(), nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("start_time_local", sa.Time(), nullable=False),
            sa.Column("end_time_local", sa.Time(), nullable=False),
            sa.ForeignKeyConstraint(["schedule_id"], ["staff_weekly_schedules.id"]),
        )

    if "staff_break_blocks" not in table_names:
        op.create_table(
            "staff_break_blocks",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("schedule_id", sa.UUID(), nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("start_time_local", sa.Time(), nullable=False),
            sa.Column("end_time_local", sa.Time(), nullable=False),
            sa.ForeignKeyConstraint(["schedule_id"], ["staff_weekly_schedules.id"]),
        )

    if "staff_exceptions" not in table_names:
        op.create_table(
            "staff_exceptions",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("location_id", sa.UUID(), nullable=True),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("start_utc", sa.DateTime(), nullable=False),
            sa.Column("end_utc", sa.DateTime(), nullable=False),
            sa.Column(
                "is_all_day",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
            sa.Column("recurring_rule", sa.Text(), nullable=True),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("created_by", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        )

    if "booking_holds" not in table_names:
        op.create_table(
            "booking_holds",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("service_id", sa.UUID(), nullable=False),
            sa.Column("location_id", sa.UUID(), nullable=True),
            sa.Column("start_utc", sa.DateTime(), nullable=False),
            sa.Column("end_utc", sa.DateTime(), nullable=False),
            sa.Column("expires_at_utc", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.UUID(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["service_id"], ["services.id"]),
            sa.ForeignKeyConstraint(["location_id"], ["locations.id"]),
            sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        )

    if "schedule_change_requests" not in table_names:
        op.create_table(
            "schedule_change_requests",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("staff_id", sa.UUID(), nullable=False),
            sa.Column("requested_by", sa.UUID(), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=True),
            sa.Column("review_note", sa.Text(), nullable=True),
            sa.Column("reviewed_by", sa.UUID(), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["staff_id"], ["users.id"]),
            sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
            sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        )

    if "audit_logs" not in table_names:
        op.create_table(
            "audit_logs",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("actor_id", sa.UUID(), nullable=True),
            sa.Column("action", sa.String(length=50), nullable=False),
            sa.Column("entity_type", sa.String(length=50), nullable=False),
            sa.Column("entity_id", sa.UUID(), nullable=True),
            sa.Column("changes", postgresql.JSONB(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        )


def downgrade() -> None:
    """Downgrade schema."""
    return
