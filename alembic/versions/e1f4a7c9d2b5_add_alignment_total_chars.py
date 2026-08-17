"""add total_chars to book_alignments

Revision ID: e1f4a7c9d2b5
Revises: d9e7c4a1b2f6
Create Date: 2026-08-15
"""

from alembic import op
import sqlalchemy as sa


revision = "e1f4a7c9d2b5"
down_revision = "d9e7c4a1b2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Record the ebook length an alignment map was built against.

    get_progress_for_time() used the map's last anchor character as the
    denominator when converting an audio timestamp to a text fraction. That
    anchor is not the end of the book, so every audio-client position read
    high — catastrophically so for a map that only spans part of the text.
    Nullable: existing rows keep the old fallback until they are re-aligned.
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignments" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("book_alignments")}
    if "total_chars" in columns:
        return

    op.add_column("book_alignments", sa.Column("total_chars", sa.Integer(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "book_alignments" not in set(inspector.get_table_names()):
        return

    columns = {column["name"] for column in inspector.get_columns("book_alignments")}
    if "total_chars" not in columns:
        return

    op.drop_column("book_alignments", "total_chars")
