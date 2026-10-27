"""L5：events journal 補 statement-level BEFORE TRUNCATE trigger

Revision ID: 0007_journal_truncate_guard
Revises: 0006_journal_txid
Create Date: 2026-10-27

0001 的 events_append_only 只擋列級 UPDATE／DELETE，TRUNCATE 不經過列級 trigger，
可一次清空整個 journal。這裡補上語句級 trigger，與 append-only 保護一致。
測試清庫（tests/uep/conftest.py）在交易內暫時停用此 trigger；正式環境不應停用。
"""
from alembic import op

revision = "0007_journal_truncate_guard"
down_revision = "0006_journal_txid"
branch_labels = None
depends_on = None

_FUNCTION = """
CREATE FUNCTION events_no_truncate() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'events journal 為 append-only，不可 TRUNCATE';
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_FUNCTION)
    op.execute(
        "CREATE TRIGGER events_no_truncate BEFORE TRUNCATE ON events "
        "FOR EACH STATEMENT EXECUTE FUNCTION events_no_truncate()"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER events_no_truncate ON events")
    op.execute("DROP FUNCTION events_no_truncate()")
