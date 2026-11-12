"""Resume 回 Host 入口的型別（設計稿「Resume回Host入口設計」§3，D107）。

``LoopLauncher`` 在續跑的 Run 到達終態（不是再次 park）時，以注入的 callable 把 ``ResumedRunResult`` 交給
``EchoPipelineHost.accept_resumed_result``：Host 以 activity 綁定的 ``OutputIntent`` 經 Output Gate 送出（送出當下重取 Lease 授權），
送達後才寫記憶。型別只含歸屬事實與結果，不含授權欄位（授權一律在送出當下由 Host 重取）。
"""
from __future__ import annotations

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
    UNCERTAIN = "uncertain"  # UNKNOWN／沒搶到送出權：不重送、不寫記憶
    DROPPED = "dropped"  # 現在不能送（無 Lease、被 Gate 拒、無文字…），本階段不保留（pending 佇列屬後續 PR）
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
    text: str = ""  # outcome=COMPLETED 才有
    reason_code: str | None = None
