"""add missing auth schema objects

Revision ID: 20260420authfix
Revises: 20260412maploc
Create Date: 2026-04-20 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "20260420authfix"
down_revision: Union[str, Sequence[str], None] = "20260412maploc"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    user_columns = {col["name"] for col in inspector.get_columns("users")}
    table_names = set(inspector.get_table_names())

    if "email_verified" not in user_columns:
        op.add_column(
            "users",
            sa.Column(
                "email_verified",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("true"),
            ),
        )

    if "email_verification_tokens" not in table_names:
        op.create_table(
            "email_verification_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
        )
        op.create_index(
            "ix_email_verification_tokens_user_id",
            "email_verification_tokens",
            ["user_id"],
            unique=False,
        )
    else:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("email_verification_tokens")
        }
        if "ix_email_verification_tokens_user_id" not in existing_indexes:
            op.create_index(
                "ix_email_verification_tokens_user_id",
                "email_verification_tokens",
                ["user_id"],
                unique=False,
            )

    if "magic_link_tokens" not in table_names:
        op.create_table(
            "magic_link_tokens",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("token_hash", sa.String(length=64), nullable=False),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("used_at", sa.DateTime(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("now()"),
            ),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("token_hash"),
        )
        op.create_index(
            "ix_magic_link_tokens_user_id",
            "magic_link_tokens",
            ["user_id"],
            unique=False,
        )
    else:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("magic_link_tokens")
        }
        if "ix_magic_link_tokens_user_id" not in existing_indexes:
            op.create_index(
                "ix_magic_link_tokens_user_id",
                "magic_link_tokens",
                ["user_id"],
                unique=False,
            )


def downgrade() -> None:
    return
