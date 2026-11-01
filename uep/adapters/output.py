"""Output Adapter 協定（契約 CC-5）。

Adapter 只送出並回報 *證據*（EvidenceReport），不直接改 delivery 狀態；
狀態推進由 Delivery Manager 依證據決定（5.4）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from uep.output.delivery import DeliveryEvidence
from uep.output.intent import ControlSignal, OutputIntent


class SendFailed(Exception):
    """送出明確失敗（未被對端接受）。"""


class SendResponseLost(Exception):
    """已送出但 ACK 遺失或逾時；是否送達未知（O-6）。"""


@dataclass(frozen=True)
class SendResult:
    """送出當下的回報。``dispatch_id`` 為外部送出的追蹤 ID。"""

    dispatch_id: str
    evidence: DeliveryEvidence
    reported_at: datetime
    played_range: dict[str, Any] | None = None


SendLookupOutcome = Literal["found", "confirmed_not_sent", "undetermined"]


@dataclass(frozen=True)
class SendLookup:
    """查核結果三態（與 ToolExecutor 的 ReceiptLookup 對齊）。

    - found：查到已送出，``result`` 為當時的回報。
    - confirmed_not_sent：Adapter 以權威紀錄確認從未送出；僅當 ``lookup_authoritative`` 為真才可信。
    - undetermined：查不到或無法確認，維持 unknown，不得重送。
    """

    outcome: SendLookupOutcome
    result: SendResult | None = None


@runtime_checkable
class OutputAdapter(Protocol):
    @property
    def lookup_authoritative(self) -> bool:
        """Adapter 的送出紀錄是否持久且完整，足以「確認未送出」。"""
        ...

    def send(self, intent: OutputIntent, dispatch_id: str) -> SendResult:
        """送出輸出。ACK 遺失拋 SendResponseLost，明確失敗拋 SendFailed；不得自行重送。

        冪等語意：同一 ``dispatch_id`` 重複呼叫，真實 Adapter 應只產生一次外部效果（或在 ``lookup``
        中回報既有結果）；做不到者必須如實聲明。Delivery Manager 以單一送出者（claim）避免並行，
        並在接手過期 claim 後的重送前先 ``lookup``。
        """
        ...

    def control(self, signal: ControlSignal) -> None:
        """獨立控制通道；stop 同步、立即、冪等（以 control_id 去重）。"""
        ...

    def lookup(self, dispatch_id: str) -> SendLookup:
        """以 dispatch_id 查核。查核本身失聯時回 undetermined，不拋例外。"""
        ...
