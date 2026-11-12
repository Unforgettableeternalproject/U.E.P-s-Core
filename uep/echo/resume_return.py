"""Resume 回 Host 入口的型別（設計稿「Resume回Host入口設計」§3，D107）。

``LoopLauncher`` 在續跑的 Run 到達終態（不是再次 park）時，以注入的 callable 把 ``ResumedRunResult`` 交給
``EchoPipelineHost.accept_resumed_result``：Host 以 activity 綁定的 ``OutputIntent`` 經 Output Gate 送出（送出當下重取 Lease 授權），
送達後才寫記憶。型別只含歸屬事實與結果，不含授權欄位（授權一律在送出當下由 Host 重取）。
"""
from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ResumeCause(StrEnum):
    APPROVED = "approved"  # 有核准、已記 Approval
    REJECTED = "rejected"  # 先 record_rejection 再 resume（Console /reject）：收尾文字照送，不寫記憶


class ResumeOutcome(StrEnum):
    COMPLETED = "completed"  # Run 完成且有最終文字
    FAILED = "failed"  # Run 失敗（含 context_lost、預算、重驗被拒）：本階段不送、不寫
    CANCELLED = "cancelled"  # closing／cancel_requested／撤銷：不送、不寫


class ResumeDisposition(StrEnum):
    DELIVERED = "delivered"  # 已送達（視 cause）並寫記憶
    PENDING = "pending"  # 暫時不能送（無 Lease／muted／busy／聽眾比 Presence 寬…），已進 pending 佇列，之後 flush
    UNCERTAIN = "uncertain"  # UNKNOWN／沒搶到送出權：不重送、不寫記憶
    DROPPED = "dropped"  # 不會再送：TTL 到期、佇列溢出、確定失敗（SEND_FAILED 等）、無文字；不寫記憶
    NOT_APPLICABLE = "not_applicable"  # 無需回覆（FAILED／CANCELLED）


@dataclass(frozen=True)
class ResumedRunResult:
    run_id: str
    worker_epoch: int  # 與 run_id 構成冪等鍵
    task_id: str
    activity_id: str | None  # Task 的 activity（actv_…）；續跑回覆以它綁定送出
    ticket: Any  # 原 turn 的 CoreTicket：只當歸屬事實（actor／user_text／segment／audience），授權欄位不使用
    outcome: ResumeOutcome
    cause: ResumeCause
    text: str = ""  # outcome=COMPLETED 才有（FAILED 時是固定的系統訊息文字）
    reason_code: str | None = None
    seq: int = 0  # 同一個 (run_id, worker_epoch) 要送多則訊息時的序號（延後的預算通知／失敗訊息）；0＝最終結果

    @property
    def key(self) -> tuple[str, int]:
        """冪等鍵：seq=0 時就是 ``(run_id, worker_epoch)``，其餘訊息各自獨立。"""
        return (self.run_id, self.worker_epoch) if self.seq == 0 else (f"{self.run_id}#{self.seq}", self.worker_epoch)


PendingKey = tuple[str, int]  # (run_id, worker_epoch)


class ResumePending:
    """暫時送不出去的續跑結果的記憶體佇列（D107 Q5：不持久化；Q8：預設 TTL 30 分鐘、上限 8 筆）。

    不自己加鎖：由 Host 在 ``_resume_lock`` 內使用。溢出時丟最舊的一筆（回傳給呼叫端記 metric 與冪等紀錄）；
    逾時項目在每次存取時惰性清理（不新增執行緒）。被丟掉的項目不寫記憶。"""

    def __init__(self, *, ttl_s: float, max_items: int, clock: Callable[[], float] = time.monotonic) -> None:
        if ttl_s <= 0 or max_items < 1:
            raise ValueError("ttl_s 與 max_items 必須為正")
        self._ttl_s = ttl_s
        self._max = max_items
        self._clock = clock
        self._items: OrderedDict[PendingKey, tuple[float, ResumedRunResult]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def add(self, result: ResumedRunResult) -> ResumedRunResult | None:
        """加入（同鍵已在佇列則不動）；超出上限時回傳被丟掉的最舊一筆。"""
        key = result.key
        if key in self._items:
            return None
        self._items[key] = (self._clock() + self._ttl_s, result)
        if len(self._items) > self._max:
            _, (_, oldest) = self._items.popitem(last=False)
            return oldest
        return None

    def expire(self) -> list[ResumedRunResult]:
        """移除並回傳已逾時的項目。"""
        now = self._clock()
        gone = [k for k, (deadline, _) in self._items.items() if deadline <= now]
        return [self._items.pop(k)[1] for k in gone]

    def snapshot(self) -> list[ResumedRunResult]:
        """目前的項目（最舊的在前）。"""
        return [result for _, result in self._items.values()]

    def remove(self, key: PendingKey) -> None:
        self._items.pop(key, None)
