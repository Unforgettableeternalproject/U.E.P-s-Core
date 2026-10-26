"""Engine 與 Session 工廠。僅支援 PostgreSQL（A9／D10），驅動為 psycopg3。"""
from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from uep.config import get_settings


def normalize_url(url: str) -> str:
    """將 ``postgresql://`` 或 ``postgres://`` 正規化為 psycopg3 驅動。"""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix):]
    return url


def make_engine(url: str | None = None) -> Engine:
    """建立 Engine；未傳入 url 時讀 UEP_DATABASE_URL。"""
    configured = get_settings().database_url
    resolved = url or (configured.get_secret_value() if configured else None)
    if not resolved:
        raise RuntimeError("未設定資料庫連線字串（UEP_DATABASE_URL）")
    return create_engine(normalize_url(resolved), pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)
