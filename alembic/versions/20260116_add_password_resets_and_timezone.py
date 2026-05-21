"""add password resets and user timezone

Revision ID: 20260116addpw
Revises: 20260115addauth
Create Date: 2026-01-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "20260116addpw"
down_revision: Union[str, Sequence[str], None] = "20260115addauth"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    user_columns = {col["name"] for col in inspector.get_columns("users")}
    table_names = set(inspector.get_table_names())

    if "timezone" not in user_columns:
        op.add_column(
            "users",
            sa.Column("timezone", sa.String(length=50), nullable=True),
        )

    if "password_reset_tokens" not in table_names:
        op.create_table(
            "password_reset_tokens",
            sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.UUID(), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False, unique=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        )
        op.create_index(
            "ix_password_reset_tokens_user_id",
            "password_reset_tokens",
            ["user_id"],
            unique=False,
        )
    else:
        index_names = {idx["name"] for idx in inspector.get_indexes("password_reset_tokens")}
        if "ix_password_reset_tokens_user_id" not in index_names:
            op.create_index(
                "ix_password_reset_tokens_user_id",
                "password_reset_tokens",
                ["user_id"],
                unique=False,
            )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_names = set(inspector.get_table_names())

    if "password_reset_tokens" in table_names:
        index_names = {idx["name"] for idx in inspector.get_indexes("password_reset_tokens")}
        if "ix_password_reset_tokens_user_id" in index_names:
            op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
        op.drop_table("password_reset_tokens")
