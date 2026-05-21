"""migrate UTC timezone defaults to Asia/Phnom_Penh

Revision ID: 20260401utcmigrate
Revises: 20260209addcore
Create Date: 2026-04-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
from sqlalchemy import text


# revision identifiers, used by Alembic.
revision: str = "20260401utcmigrate"
down_revision: Union[str, Sequence[str], None] = "20260209addcore"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TARGET_TZ = "Asia/Phnom_Penh"
OLD_TZ = "UTC"


def upgrade() -> None:
    """Replace UTC timezone placeholders with Asia/Phnom_Penh."""
    conn = op.get_bind()

    conn.execute(text(f"UPDATE bookings SET customer_timezone = '{TARGET_TZ}' WHERE customer_timezone = '{OLD_TZ}'"))
    conn.execute(text(f"UPDATE customers SET timezone = '{TARGET_TZ}' WHERE timezone = '{OLD_TZ}'"))
    conn.execute(text(f"UPDATE users SET timezone = '{TARGET_TZ}' WHERE timezone = '{OLD_TZ}'"))
    conn.execute(text(f"UPDATE staff_weekly_schedules SET timezone = '{TARGET_TZ}' WHERE timezone = '{OLD_TZ}'"))
    conn.execute(text(f"UPDATE locations SET timezone = '{TARGET_TZ}' WHERE timezone = '{OLD_TZ}'"))

    try:
        conn.execute(text(f"UPDATE availability_rules SET timezone = '{TARGET_TZ}' WHERE timezone = '{OLD_TZ}'"))
    except Exception:
        pass


def downgrade() -> None:
    """Revert Asia/Phnom_Penh back to UTC (best-effort)."""
    conn = op.get_bind()
    for table, col in [
        ("bookings", "customer_timezone"),
        ("customers", "timezone"),
        ("users", "timezone"),
        ("staff_weekly_schedules", "timezone"),
        ("locations", "timezone"),
    ]:
        conn.execute(text(f"UPDATE {table} SET {col} = '{OLD_TZ}' WHERE {col} = '{TARGET_TZ}'"))
