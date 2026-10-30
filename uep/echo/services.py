"""``assemble_core`` 在 ``bind`` 時交給 ES 宿主的 Core 服務（不依賴 echo_stream，組裝層可安全 import）。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from uep.endpoint.protocol import CoreApi
from uep.state.coordinator import StateCoordinator


@dataclass
class EchoServices:
    core_api: CoreApi
    state: StateCoordinator
    control: Any
    session_factory: sessionmaker[Session]
    delivery: Any
    gate: Any = None
    presence_audience: Any = None  # Gate 的 audience provider（ALLOW_ALL 時為 None）；須是只讀記憶體的 callable
