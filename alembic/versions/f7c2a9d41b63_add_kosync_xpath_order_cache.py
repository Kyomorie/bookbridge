"""persist KoSync canonical XPath ordering cache

Revision ID: f7c2a9d41b63
Revises: e1f4a7c9d2b5
Create Date: 2026-08-20
"""

from alembic import op
import sqlalchemy as sa

revision = "f7c2a9d41b63"
down_revision = "e1f4a7c9d2b5"
branch_labels = None
depends_on = None

_TABLE = "kosync_xpath_order_cache"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("key_hash", sa.String(length=64), nullable=False),
        sa.Column("document_hash", sa.String(length=32), nullable=False),
        sa.Column("filename", sa.String(length=500), nullable=False),
        sa.Column("device_xpath", sa.Text(), nullable=False),
        sa.Column("synced_xpath", sa.Text(), nullable=False),
        sa.Column("device_index", sa.Integer(), nullable=False),
        sa.Column("synced_index", sa.Integer(), nullable=False),
        sa.Column("file_key", sa.String(length=64), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("key_hash", name="uq_kosync_xpath_order_cache_key"),
    )
    op.create_index(
        "ix_kosync_xpath_order_cache_document_hash",
        _TABLE,
        ["document_hash"],
        unique=False,
    )
    op.create_index(
        "ix_kosync_xpath_order_cache_updated_at",
        _TABLE,
        ["updated_at"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        op.drop_table(_TABLE)
