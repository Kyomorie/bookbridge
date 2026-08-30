"""add per-user pending KoSync -> ABS rewind decisions

Revision ID: b6d4e2f8a1c3
Revises: 2e0a47a3dadd
Create Date: 2026-08-30
"""

from alembic import op
import sqlalchemy as sa

revision = "b6d4e2f8a1c3"
down_revision = "2e0a47a3dadd"
branch_labels = None
depends_on = None

_TABLE = "pending_rewinds"
_UNIQUE = "uq_pending_rewinds_user_book_source"
_STATUS_INDEX = "ix_pending_rewinds_user_status_expires"


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
        sa.Column("source_client", sa.String(length=32), nullable=False, server_default="kosync"),
        sa.Column("source_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_json", sa.Text(), nullable=False),
        sa.Column("target_snapshot_json", sa.Text(), nullable=False),
        sa.Column("proposed_abs_ts", sa.Float(), nullable=False),
        sa.Column("proposed_pct", sa.Float(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False),
        sa.Column("decided_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("user_id", "abs_id", "source_fingerprint", name=_UNIQUE),
    )
    op.create_index(_STATUS_INDEX, _TABLE, ["user_id", "status", "expires_at"], unique=False)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _STATUS_INDEX in existing_indexes:
        op.drop_index(_STATUS_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
