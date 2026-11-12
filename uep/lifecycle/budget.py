"""資源預算與外部壓力判斷（Module Coordinator 設計稿 §7.3、§7.4；D109 R6／R7／R11；PR-3）。

只放純邏輯與 probe 介面，不含 runtime 狀態機。coordinator 在鎖外取樣（``ResourceBudget.refresh``），在鎖內只讀快取的樣本。

放行公式（§7.3）::

    有效空閒 = min( 預算上限 − Σ 已持有的宣告值 ,  實測空閒 − 保留水位 − Σ 樣本尚未反映的預留 )

- 預算上限：設了 ``budget_mb`` 取之；沒設取「實測總量 − 保留水位」。
- 兩側各算一次、取較保守者，所以載入中的預留與外部占用都只被扣一次（預算側只扣宣告值；實測側本來就含外部占用，
  只額外扣「樣本時間之後才成立」或「仍在載入中」的預留）。
- 成本未知、樣本缺失／過期／量測失敗、水位壓力中：拒絕新增 GPU 載入（fail-closed）；不需 GPU 的服務不經過這裡。

預算管理未設定（沒有 ``BudgetConfig``）= 不啟用：coordinator 不建立 ``ResourceBudget``，需要 GPU 且會由 coordinator 載入的模組
一律被拒（``gpu_budget_unconfigured``），不需 GPU 的服務行為與 PR-2 相同。
"""
from __future__ import annotations

import importlib
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger("uep.lifecycle")

# 放行拒絕原因（閉集短碼；會進 counters 與 RequestResult.reason）
REJECT_UNCONFIGURED = "gpu_budget_unconfigured"
REJECT_UNMEASURED_COST = "unmeasured_cost"
REJECT_MEASUREMENT_FAILED = "measurement_failed"
REJECT_MEASUREMENT_STALE = "measurement_stale"
REJECT_PRESSURE = "pressure"
REJECT_EVICT_HOLD = "evict_hold"
REJECT_INSUFFICIENT = "insufficient_resource"


class ProbeUnavailable(RuntimeError):
    """量測失敗（沒有 pynvml、沒有 GPU、驅動錯誤等）；訊息不含環境細節。"""


@dataclass(frozen=True)
class VramReading:
    """一次 VRAM 量測（MB）。``free_mb`` 已包含所有行程（含 coordinator 管不到的外部使用者）。"""

    total_mb: int
    free_mb: int

    def __post_init__(self) -> None:
        if self.total_mb < 0 or self.free_mb < 0 or self.free_mb > self.total_mb:
            raise ValueError("VramReading 必須滿足 0 <= free_mb <= total_mb")


@runtime_checkable
class VramProbe(Protocol):
    """VRAM 量測介面；``read`` 必須快速返回（在 coordinator 的排程執行緒上、鎖外呼叫）。失敗拋 ``ProbeUnavailable``。"""

    def read(self) -> VramReading: ...


class NvmlVramProbe:
    """以 NVML 量測指定 GPU 的 VRAM。``pynvml`` 延遲載入，沒裝只會讓 ``read`` 拋 ``ProbeUnavailable``，不影響 import。

    注意：NVML 不理會 ``CUDA_VISIBLE_DEVICES``；``device_index`` 是實體索引。測試不得使用本類別讀真 GPU。"""

    def __init__(self, device_index: int = 0, *, module_loader: Callable[[str], object] | None = None) -> None:
        if device_index < 0:
            raise ValueError("device_index 不得為負")
        self._index = device_index
        self._loader = module_loader or importlib.import_module
        self._nvml: object | None = None
        self._lock = threading.Lock()

    def _ensure(self) -> object:
        with self._lock:
            if self._nvml is not None:
                return self._nvml
            try:
                nvml = self._loader("pynvml")
                nvml.nvmlInit()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001  ImportError、NVMLError 等一律視為量測不可用
                raise ProbeUnavailable(f"nvml_init:{type(exc).__name__}") from None
            self._nvml = nvml
            return nvml

    def read(self) -> VramReading:
        nvml = self._ensure()
        try:
            handle = nvml.nvmlDeviceGetHandleByIndex(self._index)  # type: ignore[attr-defined]
            info = nvml.nvmlDeviceGetMemoryInfo(handle)  # type: ignore[attr-defined]
            return VramReading(total_mb=int(info.total) // (1024 * 1024), free_mb=int(info.free) // (1024 * 1024))
        except Exception as exc:  # noqa: BLE001
            raise ProbeUnavailable(f"nvml_read:{type(exc).__name__}") from None


@dataclass(frozen=True)
class BudgetConfig:
    """預算設定（``UEP_MODULE_*``；預設值需 Bernie 依機器量測後決定，程式不猜）。

    - ``headroom_mb``：保留水位，留給訓練、bot、Ollama 等 coordinator 管不到的外部使用者。
    - ``budget_mb``：預算上限；None 取「實測總量 − 保留水位」。
    - ``reading_max_age_s``：樣本超過此秒數視為過期。
    - ``recover_margin_mb``：壓力回復水位 = 保留水位 + 此值（遲滯，避免在水位線上反覆進出）。
    - ``evict_cooldown_s``：每次壓力收回之後，下一次收回／被收回模組重載的最短間隔。
    """

    headroom_mb: int
    budget_mb: int | None = None
    reading_max_age_s: float = 5.0
    recover_margin_mb: int = 256
    evict_cooldown_s: float = 60.0

    def __post_init__(self) -> None:
        for key in ("headroom_mb", "recover_margin_mb"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{key} 必須是非負整數")
        if self.budget_mb is not None and (isinstance(self.budget_mb, bool) or not isinstance(self.budget_mb, int) or self.budget_mb <= 0):
            raise ValueError("budget_mb 必須是正整數或 None")
        for key in ("reading_max_age_s", "evict_cooldown_s"):
            value = getattr(self, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"{key} 必須是非負數")
        if self.reading_max_age_s == 0:
            raise ValueError("reading_max_age_s 必須大於 0")


@dataclass(frozen=True)
class Admission:
    ok: bool
    reason: str | None = None
    effective_free_mb: int | None = None


class ResourceBudget:
    """樣本快取＋放行／壓力判斷。``refresh`` 在鎖外呼叫；其餘方法只讀快取，可在 coordinator 鎖內呼叫（不阻塞）。"""

    def __init__(self, config: BudgetConfig, probe: VramProbe) -> None:
        self._config = config
        self._probe = probe
        self._lock = threading.Lock()
        self._reading: VramReading | None = None
        self._taken_at: float | None = None
        self._failed = False
        self._pressure = False
        self.failures = 0  # 累計量測失敗次數（觀測用）

    @property
    def config(self) -> BudgetConfig:
        return self._config

    # --- 取樣（鎖外）---

    def refresh(self, now: float) -> bool:
        """量測一次並以 ``now``（coordinator 的時鐘）標記時間戳、更新快取；失敗時保留舊樣本（會自然過期）並標記失敗。回傳是否成功。"""
        try:
            reading = self._probe.read()
            if not isinstance(reading, VramReading):
                raise ProbeUnavailable("bad_reading")
        except Exception as exc:  # noqa: BLE001  任何量測例外都視為失敗，不外溢內容
            with self._lock:
                self._failed = True
                self.failures += 1
            logger.warning("VRAM 量測失敗：%s；新增 GPU 載入將被拒絕", type(exc).__name__)
            return False
        with self._lock:
            self._reading, self._taken_at, self._failed = reading, now, False
        return True

    # --- 判斷（讀快取）---

    def reading(self) -> tuple[VramReading | None, float | None]:
        with self._lock:
            return self._reading, self._taken_at

    def _fresh_locked(self, now: float) -> tuple[VramReading | None, str | None]:
        if self._failed or self._reading is None or self._taken_at is None:
            return None, REJECT_MEASUREMENT_FAILED
        if now - self._taken_at > self._config.reading_max_age_s:
            return None, REJECT_MEASUREMENT_STALE
        return self._reading, None

    def fresh_reading(self, now: float) -> tuple[VramReading | None, float | None]:
        """未過期、且最近一次量測成功的樣本與其時間戳；否則 (None, None)。"""
        with self._lock:
            reading, bad = self._fresh_locked(now)
            return (reading, self._taken_at) if bad is None else (None, None)

    def update_pressure(self, now: float) -> bool:
        """以遲滯更新壓力旗標：實測空閒 < 保留水位進入；≥ 保留水位 + 回復餘裕離開。樣本不可用時保持原值（不憑舊資料動作）。"""
        with self._lock:
            reading, bad = self._fresh_locked(now)
            if bad is None and reading is not None:
                if not self._pressure and reading.free_mb < self._config.headroom_mb:
                    self._pressure = True
                elif self._pressure and reading.free_mb >= self._config.headroom_mb + self._config.recover_margin_mb:
                    self._pressure = False
            return self._pressure

    @property
    def under_pressure(self) -> bool:
        with self._lock:
            return self._pressure

    def evaluate(self, *, cost_mb: int | None, held_mb: int, pending_mb: int, now: float) -> Admission:
        """某個需要 GPU 的載入是否放行。

        ``held_mb``：所有仍持有資源的 GPU 模組宣告值總和（含載入中、未確認釋放者）；``pending_mb``：其中樣本尚未反映的部分。
        """
        if cost_mb is None:
            return Admission(False, REJECT_UNMEASURED_COST)
        with self._lock:
            reading, bad = self._fresh_locked(now)
            if bad is not None or reading is None:
                return Admission(False, bad or REJECT_MEASUREMENT_FAILED)
            if self._pressure:
                return Admission(False, REJECT_PRESSURE)
            cap = self._config.budget_mb if self._config.budget_mb is not None else reading.total_mb - self._config.headroom_mb
            effective = min(cap - held_mb, reading.free_mb - self._config.headroom_mb - pending_mb)
            if cost_mb > effective:
                return Admission(False, REJECT_INSUFFICIENT, effective)
            return Admission(True, None, effective)
