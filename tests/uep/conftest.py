"""tests/uep 共用 fixture。

DB 測試只使用 UEP_TEST_DATABASE_URL；未設定時 skip（不以 SQLite 代替）。
每個測試前 TRUNCATE CC-1 資料表；且只允許庫名以 _test 結尾，且不得與 UEP_DATABASE_URL 相同。

隔離 repo 根 ``.env``：載入本檔時即讓 ``UepSettings`` 不再讀 env_file，測試只看環境變數與預設值，
不被開發者本機 ``.env`` 污染。唯一例外是 DB URL（白名單：``UEP_TEST_DATABASE_URL``，比對用的
``UEP_DATABASE_URL`` 亦同），取得順序見 ``resolve_db_urls``。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from pydantic import SecretStr
from sqlalchemy import make_url, text
from sqlalchemy.exc import ArgumentError

import uep.db.models_registry  # noqa: F401  確保 Base.metadata 含所有資料表
from uep.config import UepSettings, get_settings
from uep.db.engine import make_engine, make_session_factory, normalize_url

REPO_ROOT = Path(__file__).resolve().parents[2]

# 測試不讀 repo 根 .env：覆寫 model_config 的 env_file，並清掉可能已建立的 get_settings() 單例
UepSettings.model_config["env_file"] = None
get_settings.cache_clear()

TEST_DB_KEY = "UEP_TEST_DATABASE_URL"
PROD_DB_KEY = "UEP_DATABASE_URL"
CC6_TABLES = "receipts, action_intents, approvals, steps, runs, tasks, activities"
CC1_TABLES = "inbox, outbox, events, payload_blobs, context_snapshots, output_intents, deliveries"
CC4_TABLES = "presence_leases, presence_transfers, presence_current"
CC7_TABLES = "state_requests, state_projection, global_stop_flags"
DECISION_TABLES = "decision_log"  # DL-3：console rig 會寫入決策日誌
PLAN_TABLES = "presence_plan_slot_marks, presence_plan_overrides, presence_plan_items, presence_plan_meta"  # PP-1


def alembic_config(url: str) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "uep" / "db" / "migrations"))
    cfg.attributes["url"] = url
    return cfg


@pytest.fixture(autouse=True)
def payload_hash_key(monkeypatch) -> str:
    """預設提供測試用的 payload_hash 密鑰（D42：沒有密鑰就拒寫私人 payload）；需要「未設定」的測試自行覆寫為 None。"""
    key = "test-only-payload-hash-key-0123456789"
    monkeypatch.setattr(get_settings(), "payload_hash_key", SecretStr(key))
    return key


@pytest.fixture(autouse=True)
def agent_loop_dir(monkeypatch, tmp_path) -> Path:
    """agent loop 持久 context store 一律落在 tmp_path，測試不寫入使用者家目錄（~/.uep/agent_loop）。"""
    path = tmp_path / "agent_loop"
    monkeypatch.setattr(get_settings(), "agent_loop_dir", path)
    return path


@pytest.fixture(autouse=True)
def chatroom_cursor_dir(monkeypatch, tmp_path) -> Path:
    """chatroom 工具的游標檔一律落在 tmp_path，測試不寫入使用者家目錄（~/.uep/chatroom_cursors）。"""
    path = tmp_path / "chatroom_cursors"
    monkeypatch.setattr(get_settings(), "chatroom_cursor_dir", path)
    return path


def _read_dotenv_key(key: str, env_path: Path | None = None) -> str | None:
    """只從 ``.env`` 讀單一鍵（白名單）；其他鍵一律不取。檔案不存在或鍵不存在回 None。"""
    path = env_path if env_path is not None else REPO_ROOT / ".env"
    try:
        if not path.is_file():
            return None
        return dotenv_values(path, encoding="utf-8").get(key)
    except OSError:
        return None


def resolve_db_urls(env_path: Path | None = None) -> tuple[str | None, str, str | None]:
    """回傳 (測試庫 URL, 來源, 正式庫 URL)。

    測試庫：環境變數 ``UEP_TEST_DATABASE_URL`` 有設就用（空字串 = 明確離線，來源 ``env``、URL 為 None）；
    沒設才從 ``.env`` 白名單讀同一鍵；都沒有則來源 ``none``。正式庫 URL 只用於比對，同樣先環境變數再 ``.env``；
環境變數設為空字串時視為明確沒有正式庫，不再回退讀 ``.env``。
    """
    if TEST_DB_KEY in os.environ:
        test_url, source = os.environ[TEST_DB_KEY].strip() or None, "env"
    else:
        test_url = (_read_dotenv_key(TEST_DB_KEY, env_path) or "").strip() or None
        source = ".env" if test_url else "none"
    prod_url = (os.environ.get(PROD_DB_KEY) or "").strip() or None
    if prod_url is None and PROD_DB_KEY not in os.environ:
        prod_url = (_read_dotenv_key(PROD_DB_KEY, env_path) or "").strip() or None
    return test_url, source, prod_url


def pytest_report_header(config) -> str:
    """報告標頭：說明測試庫來源與庫名；絕不輸出帳密或完整 URL。"""
    url, source, _ = resolve_db_urls()
    if url is None:
        return f"uep test db: source={source} (離線，DB 測試將 skip)"
    try:
        name = make_url(normalize_url(url)).database or "?"
    except (ArgumentError, ValueError):  # 畸形 URL（如非數字埠號）不得讓 pytest 啟動失敗
        name = "?"
    return f"uep test db: source={source} database={name}"


@pytest.fixture(scope="session")
def test_db_url() -> str:
    url, _, prod_url = resolve_db_urls()
    if not url:
        pytest.skip("未設定 UEP_TEST_DATABASE_URL，略過需要 PostgreSQL 的測試")
    try:
        target = make_url(normalize_url(url))
        prod_target = make_url(normalize_url(prod_url)) if prod_url else None
    except (ArgumentError, ValueError):
        pytest.fail("UEP_TEST_DATABASE_URL／UEP_DATABASE_URL 格式不正確")  # 不帶 URL 內容，避免洩漏帳密
    if prod_target is not None and prod_target == target:
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
        # 0007 的 events TRUNCATE guard：清庫時於同一交易內暫停用，結束即恢復
        conn.execute(text("ALTER TABLE events DISABLE TRIGGER events_no_truncate"))
        conn.execute(text(f"TRUNCATE {CC1_TABLES}, {CC6_TABLES}, {CC4_TABLES}, {CC7_TABLES}, {DECISION_TABLES} RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE events ENABLE TRIGGER events_no_truncate"))
        conn.execute(text(f"TRUNCATE {PLAN_TABLES} RESTART IDENTITY CASCADE"))
    return make_session_factory(engine)
