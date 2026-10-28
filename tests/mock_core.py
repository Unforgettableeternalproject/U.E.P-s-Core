"""
測試用的假 Core（M6）。

FakeCore：實作單一 emit(draft: EventDraft)，記錄收到的草稿；
  events＝memory.* 草稿、candidates＝proactive.trigger_observed 草稿。
  task_calls／presence_calls／persona_writes 計數器必須維持 0
  （Echo 不得驅動任務、存在感或寫人格）。
HostileCore：丟例外、回傳指令物件、阻塞逾時——證明 Echo 不讀回傳值、不受影響。
"""

from __future__ import annotations

import threading
from typing import Any

from echo_memory.schemas.events import EVENT_TYPE_TRIGGER_OBSERVED, EventDraft


class FakeCore:
    """正常的接收端；只記錄。"""

    def __init__(self) -> None:
        self.drafts: list[EventDraft] = []
        self.task_calls = 0
        self.presence_calls = 0
        self.persona_writes = 0

    @property
    def events(self) -> list[EventDraft]:
        return [d for d in self.drafts if d.event_type.startswith("memory.")]

    @property
    def candidates(self) -> list[EventDraft]:
        return [d for d in self.drafts if d.event_type == EVENT_TYPE_TRIGGER_OBSERVED]

    def emit(self, draft: EventDraft) -> None:
        self.drafts.append(draft)

    # Echo 不應該呼叫到下列介面；被呼叫即計數，測試斷言維持 0
    def create_task(self, *args: Any, **kwargs: Any) -> None:
        self.task_calls += 1

    def set_presence(self, *args: Any, **kwargs: Any) -> None:
        self.presence_calls += 1

    def write_persona(self, *args: Any, **kwargs: Any) -> None:
        self.persona_writes += 1


class CommandObject:
    """敵意回傳值：看起來像要 Echo 照做的指令。"""

    action = "modify_persona"
    command = "write_persona"

    def __call__(self, *args: Any, **kwargs: Any) -> None:  # 被呼叫就是 Echo 在執行指令
        raise AssertionError("Echo 不應執行 Core 回傳的指令物件")


class HostileCore(FakeCore):
    """mode：raise（丟例外）／command（回傳指令物件）／block（阻塞到被釋放）。"""

    def __init__(self, mode: str = "raise") -> None:
        super().__init__()
        self.mode = mode
        self.calls = 0
        self.release = threading.Event()

    def emit(self, draft: EventDraft) -> Any:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("hostile core failure")
        if self.mode == "command":
            return CommandObject()
        if self.mode == "block":
            self.release.wait(timeout=5.0)
        return None
