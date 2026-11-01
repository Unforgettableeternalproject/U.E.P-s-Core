"""EchoPipelineHost 的輸出設定（P3接入方案 §5.3 S4 註記；D73、D74、D82）。

不依賴 echo_stream（組裝層與測試可安全 import）。全部為不可變 dataclass，改設定請用 ``dataclasses.replace``。

- ``commit_mode``：ES 歷史寫回時機。``settled``（預設，現況）播完或確認停止後才 ``commit(spoken, generated)``，
  歷史本來就只留已播出部分，但 think 鎖會持有到結算；``eager`` 文字生成完立刻 commit 全文，播放結算後再 ``settle``
  （多輪重疊的宿主用，ES 介面清單 D8）。
- ``history_spoken_only``（D82 (b)）：``eager`` 下讓歷史最終只留 spoken，讓位於 eager commit 之後時撤回整輪未播部分。
  預設 False（維持現況）。``settled`` 下無作用（已是 spoken-only，讓位時也沒有歷史）。
  被拒、撤銷、停止、dispatch 失敗的輪不受這個開關影響，一律 ``cancel_turn(commit="none")`` 整輪撤回。
- ``dispatch_budget_ms``（D73 (A) 暫定值，S6 量測後校正）：一句從第一個 chunk 寫入 sink，到該句 intent 完成
  ``DeliveryManager.dispatch`` 的增量上限。dispatch 在工作緒進行、不擋 ES 的音訊路徑，超出只記 metric
  （``echo.dispatch_over_budget``）與最大值，不丟棄、不降級。
- ``progress_poll_interval_s``：播放進度在 ES loop 上的輕量輪詢間隔（秒）。TTS 比播放快時（所有 chunk 一次寫完）
  沒有後續 ``write`` 可觸發進度回報，靠輪詢讓已播畢的句子即時升證據（V08）。0 = 不輪詢（只在 write 與輪結束時回報）。
"""
from __future__ import annotations

from dataclasses import dataclass

COMMIT_MODES = ("settled", "eager")
DEFAULT_DISPATCH_BUDGET_MS = 150.0


@dataclass(frozen=True)
class EchoOutputConfig:
    commit_mode: str = "settled"
    history_spoken_only: bool = False
    dispatch_budget_ms: float = DEFAULT_DISPATCH_BUDGET_MS
    progress_poll_interval_s: float = 0.25

    def __post_init__(self) -> None:
        if self.commit_mode not in COMMIT_MODES:
            raise ValueError(f"commit_mode 必須是 {COMMIT_MODES} 之一：{self.commit_mode!r}")
        if self.dispatch_budget_ms < 0:
            raise ValueError("dispatch_budget_ms 不可為負")
        if self.progress_poll_interval_s < 0:
            raise ValueError("progress_poll_interval_s 不可為負")
