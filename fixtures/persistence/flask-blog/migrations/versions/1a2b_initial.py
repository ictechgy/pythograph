"""Alembic 마이그레이션(과거 스키마). pythograph는 읽지 않는다."""

from alembic import op


def upgrade():
    """과거 테이블."""
    op.execute("CREATE TABLE old_drafts (id integer)")
