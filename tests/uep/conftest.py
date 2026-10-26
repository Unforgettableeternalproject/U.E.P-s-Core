"""tests/uep 共用 fixture。

DB 測試只使用 UEP_TEST_DATABASE_URL；未設定時 skip（不以 SQLite 代替）。
每個測試前 TRUNCATE CC-1 資料表；且只允許庫名以 _test 結尾，且不得與 UEP_DATABASE_URL 相同。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text

import uep.db.models_registry  # noqa: F401  確保 Base.metadata 含所有資料表
from uep.config import get_settings
from uep.db.engine import make_engine, make_session_factory, normalize_url

REPO_ROOT = Path(__file__).resolve().parents[2]
CC6_TABLES = "receipts, action_intents, approvals, steps, runs, tasks, activities"
CC1_TABLES = "inbox, outbox, events, payload_blobs, context_snapshots, output_intents, deliveries"


def alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "uep" / "db" / "migrations"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(scope="session")
def test_db_url() -> str:
    settings = get_settings()
    secret = settings.test_database_url
    if not secret:
        pytest.skip("未設定 UEP_TEST_DATABASE_URL，略過需要 PostgreSQL 的測試")
    url = secret.get_secret_value()
    target = make_url(normalize_url(url))
    if settings.database_url and make_url(normalize_url(settings.database_url.get_secret_value())) == target:
        pytest.fail("UEP_TEST_DATABASE_URL 不得與 UEP_DATABASE_URL 相同")
    if not (target.database or "").endswith("_test"):
        pytest.fail("測試庫名稱必須以 _test 結尾（allowlist）")
    return url


@pytest.fixture(scope="session")
def alembic_cfg(test_db_url) -> Config:
    return alembic_config(test_db_url)


@pytest.fixture(scope="session")
def engine(test_db_url):
    command.upgrade(alembic_config(test_db_url), "head")
    eng = make_engine(test_db_url)
    yield eng
    eng.dispose()


@pytest.fixture
def session_factory(engine):
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {CC1_TABLES}, {CC6_TABLES} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)
