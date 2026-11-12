"""AgentLoop 的重試政策（SYS 契約 SV-2(4) 把重試政策留給 Agent Core）。

- 模型呼叫：可重試的暫時性錯誤（``TIMEOUT``／``PROVIDER_UNAVAILABLE``／``RATE_LIMITED``，且事件標 ``retryable``）或單一 Step 逾時，
  以退避重新開一個 attempt（從已提交的 history 重來，未完成的串流內容丟棄）。連續失敗次數有上限，每次成功推進後歸零。
- 工具呼叫：只重試 ``internal_read`` 且 receipt 的 ``error_code`` 屬暫時性者；重試是**新 action**（``retry_of`` 指向失敗的那一個，
  一個 action 至多被重試一次）。``reversible_local``／``external_effect``／``high_impact`` 一律不自動重試。
- 預設全關（次數 0）：``LoopBudget`` 之外獨立設定，由組裝層依 ``UEP_AGENT_*`` 環境變數建立。
"""
from __future__ import annotations

from dataclasses import dataclass

# 可重試的 runtime 錯誤碼（對應 RuntimeEvent.error.code；``retryable`` 為真才重試）
RETRYABLE_RUNTIME_CODES = frozenset({"TIMEOUT", "PROVIDER_UNAVAILABLE", "RATE_LIMITED"})
# 可重試的工具 receipt error_code（executor 簽回的短碼）；其他失敗視為確定性失敗，不重試
TRANSIENT_TOOL_ERRORS = frozenset({"timeout", "unavailable", "rate_limited"})
# 只有純讀取類可自動重試（external_effect／high_impact 一律不重試；reversible_local 無法證明冪等，保守排除）
RETRYABLE_SIDE_EFFECTS = frozenset({"internal_read"})
MAX_BACKOFF_S = 30.0


@dataclass(frozen=True)
class RetryPolicy:
    model_max: int = 0  # 連續模型呼叫失敗的重試上限
    model_backoff_s: float = 1.0
    tool_max: int = 0  # 單一工具呼叫的重試上限（目前實作至多 1 次：一個 action 只能被重試一次）
    tool_backoff_s: float = 0.5

    def __post_init__(self) -> None:
        if self.model_max < 0 or self.tool_max < 0:
            raise ValueError("重試次數不可為負")
        if self.model_backoff_s < 0 or self.tool_backoff_s < 0:
            raise ValueError("退避秒數不可為負")

    @staticmethod
    def backoff(base_s: float, attempt: int) -> float:
        """第 ``attempt``（從 1 起）次重試前的等待秒數：指數退避、有上限。"""
        return min(base_s * (2 ** max(attempt - 1, 0)), MAX_BACKOFF_S)
