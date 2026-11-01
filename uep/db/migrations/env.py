"""Alembic 環境：URL 優先取 ``config.attributes["url"]``（測試注入），否則讀 UEP_DATABASE_URL。"""
from alembic import context
from sqlalchemy import create_engine, pool

import uep.db.models_registry  # noqa: F401  確保 metadata 含所有資料表
from uep.config import get_settings
from uep.db.base import Base
from uep.db.engine import normalize_url

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    configured = get_settings().database_url
    url = config.attributes.get("url") or (configured.get_secret_value() if configured else None)
    if not url:
        raise RuntimeError("未設定資料庫連線字串（UEP_DATABASE_URL）")
    return normalize_url(url)


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = create_engine(_url(), poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
