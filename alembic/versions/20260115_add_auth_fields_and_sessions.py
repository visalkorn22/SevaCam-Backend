"""add auth fields to users and create sessions table

Revision ID: 20260115addauth
Revises: 08e1d3ea39cc
Create Date: 2026-01-15 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260115addauth"
down_revision: Union[str, Sequence[str], None] = "08e1d3ea39cc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {col["name"] for col in inspector.get_columns("users")}
    table_names = set(inspector.get_table_names())

    if "full_name" not in user_columns:
        op.add_column("users", sa.Column("full_name", sa.String(length=150), nullable=True))
    if "phone" not in user_columns:
        op.add_column("users", sa.Column("phone", sa.String(length=50), nullable=True))
    if "avatar_url" not in user_columns:
        op.add_column("users", sa.Column("avatar_url", sa.String(length=255), nullable=True))
    if "password_hash" not in user_columns:
        op.add_column("users", sa.Column("password_hash", sa.String(length=255), nullable=True))

    if "sessions" not in table_names:
        op.create_table(
            "sessions",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("token", sa.String(length=64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index("ix_sessions_token", "sessions", ["token"], unique=True)
        op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)
    else:
        index_names = {idx["name"] for idx in inspector.get_indexes("sessions")}
        if "ix_sessions_token" not in index_names:
            op.create_index("ix_sessions_token", "sessions", ["token"], unique=True)
        if "ix_sessions_user_id" not in index_names:
            op.create_index("ix_sessions_user_id", "sessions", ["user_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    return
