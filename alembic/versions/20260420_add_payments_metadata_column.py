"""add payments metadata column

Revision ID: 20260420paymeta
Revises: 20260420logdetails
Create Date: 2026-04-20 01:20:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260420paymeta"
down_revision: Union[str, Sequence[str], None] = "20260420logdetails"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = set(inspector.get_table_names())
    if "payments" not in table_names:
        return

    column_names = {col["name"] for col in inspector.get_columns("payments")}
    if "metadata" not in column_names:
        op.add_column(
            "payments",
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    table_names = set(inspector.get_table_names())
    if "payments" not in table_names:
        return

    column_names = {col["name"] for col in inspector.get_columns("payments")}
    if "metadata" in column_names:
        op.drop_column("payments", "metadata")
