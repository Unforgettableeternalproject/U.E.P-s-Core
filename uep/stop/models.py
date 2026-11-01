"""global_stop_flags（migration 0005_state_stop）：全域停止後的暫停旗標，每個 persona 一列。

旗標存 DB（跨重啟存活）：``engaged`` 時，端點不得自動重 acquire、Task 不得轉回 RUNNING，
直到操作員明確解除。只由全域停止服務寫入。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from uep.db.base import Base


class GlobalStopFlagRow(Base):
    __tablename__ = "global_stop_flags"

    persona_id: Mapped[str] = mapped_column(String, primary_key=True)
    engaged: Mapped[bool] = mapped_column(Boolean, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    engaged_by: Mapped[str | None] = mapped_column(String)
    engaged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_by: Mapped[str | None] = mapped_column(String)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False)
