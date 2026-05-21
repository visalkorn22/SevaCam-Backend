"""add staff schedule limit columns

Revision ID: 20260420schedulelimits
Revises: 20260420authfix
Create Date: 2026-04-20 00:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260420schedulelimits"
down_revision: Union[str, Sequence[str], None] = "20260420authfix"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "staff_weekly_schedules" not in table_names:
        return

    schedule_columns = {
        column["name"] for column in inspector.get_columns("staff_weekly_schedules")
    }

    if "max_slots_per_day" not in schedule_columns:
        op.add_column(
            "staff_weekly_schedules",
            sa.Column("max_slots_per_day", sa.Integer(), nullable=True),
        )
    if "max_bookings_per_day" not in schedule_columns:
        op.add_column(
            "staff_weekly_schedules",
            sa.Column("max_bookings_per_day", sa.Integer(), nullable=True),
        )
    if "max_bookings_per_customer" not in schedule_columns:
        op.add_column(
            "staff_weekly_schedules",
            sa.Column("max_bookings_per_customer", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    return
