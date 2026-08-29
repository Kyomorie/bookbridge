"""add durable pending KoSync -> ABS rewind approvals

Revision ID: b4d7e9f2c1a6
Revises: 2e0a47a3dadd
Create Date: 2026-08-29
"""

from alembic import op
import sqlalchemy as sa

revision = "b4d7e9f2c1a6"
down_revision = "2e0a47a3dadd"
branch_labels = None
depends_on = None

_TABLE = "pending_rewinds"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        return

    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("abs_id", sa.String(length=255), sa.ForeignKey("books.abs_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False, server_default="KoSync"),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("target_snapshot_json", sa.Text(), nullable=False),
        sa.Column("proposed_timestamp", sa.Float(), nullable=False),
        sa.Column("proposed_percentage", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pending_rewinds_user_id", _TABLE, ["user_id"], unique=False)
    op.create_index("ix_pending_rewinds_abs_id", _TABLE, ["abs_id"], unique=False)
    op.create_index("ix_pending_rewinds_status", _TABLE, ["status"], unique=False)
    op.create_index(
        "ix_pending_rewinds_user_abs_source",
        _TABLE,
        ["user_id", "abs_id", "source_fingerprint"],
        unique=True,
    )
    op.create_index(
        "ix_pending_rewinds_user_status_expiry",
        _TABLE,
        ["user_id", "status", "expires_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
