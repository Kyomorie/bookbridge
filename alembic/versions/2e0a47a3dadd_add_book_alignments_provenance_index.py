"""add covering index for book_alignments provenance query

Revision ID: 2e0a47a3dadd
Revises: f7c2a9d41b63
Create Date: 2026-08-24
"""

from alembic import op
import sqlalchemy as sa

revision = "2e0a47a3dadd"
down_revision = "f7c2a9d41b63"
branch_labels = None
depends_on = None

_INDEX = "ix_book_alignments_provenance"
_TABLE = "book_alignments"


def upgrade() -> None:
    """Create covering index so the provenance query reads only the index B-tree.

    get_alignment_provenance() selects abs_id, align_method, last_updated from
    book_alignments. The table's second column is alignment_map_json (Text, up to
    21 MB per row, ~1.89 GB total). SQLite must walk the overflow page chain to
    reach the trailing metadata columns, causing the SELECT to read ~1.89 GB of
    pages and hold a shared read lock long enough to make concurrent writers fail
    with "database is locked". This index makes the query index-only.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX in existing_indexes:
        return

    op.create_index(
        _INDEX,
        _TABLE,
        ["abs_id", "align_method", "last_updated"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return

    existing_indexes = {idx["name"] for idx in inspector.get_indexes(_TABLE)}
    if _INDEX not in existing_indexes:
        return

    op.drop_index(_INDEX, table_name=_TABLE)