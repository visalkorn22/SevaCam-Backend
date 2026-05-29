"""add staff profile fields to staff services

Revision ID: 20260529staffprofiles
Revises: 20260420paymeta
Create Date: 2026-05-29 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260529staffprofiles"
down_revision: Union[str, Sequence[str], None] = "20260420paymeta"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("staff_services")}

    if "skills" not in columns:
        op.add_column(
            "staff_services",
            sa.Column(
                "skills",
                postgresql.ARRAY(sa.String()),
                nullable=False,
                server_default=sa.text("'{}'::varchar[]"),
            ),
        )

    if "bio" not in columns:
        op.add_column(
            "staff_services",
            sa.Column("bio", sa.Text(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("staff_services")}

    if "bio" in columns:
        op.drop_column("staff_services", "bio")

    if "skills" in columns:
        op.drop_column("staff_services", "skills")
