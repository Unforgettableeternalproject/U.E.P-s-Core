"""S 群模組 handle 與描述子預設表（Module Coordinator 設計稿 §4、§5；PR-4）。

S 群 = 遠端服務／設定閘模組：lore、lore_delete、chatroom、web_search、web_fetch、image_read。
- ``load``／``probe`` **只驗設定，不打網路**（零網路）；可選注入 ``health`` callable（預設 None）供日後補真實探活。
- coordinator 不卸載這類模組（``can_unload=False``）；沒有資源要釋放，``unload`` 為 no-op。
- 描述子沒有 GPU 成本（``uses_gpu=False``、``vram_mb=None``）；設定面的 unavailable 仍由 ``with_*`` 對 coordinator 關閘（``blocked_reason``）。
- token 不進例外訊息或 repr：handle 只持有判定用的 callable。
"""
from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any

from uep.lifecycle.descriptor import ModuleDescriptor
from uep.lifecycle.handles import ModuleHandle, ProbeResult
from uep.tools.catalog.chatroom import CHATROOM_MODULE
from uep.tools.catalog.image import MODULE_IMAGE_READ
from uep.tools.catalog.lore import LORE_DELETE_MODULE, LORE_MODULE
from uep.tools.catalog.web import MODULE_WEB_FETCH, MODULE_WEB_SEARCH
from uep.tools.executors.chatroom import chatroom_config_from_settings
from uep.tools.executors.lore import LoreConfig
from uep.tools.web_image import image_read_available, web_search_available

logger = logging.getLogger("uep.tools.module_handles")

S_GROUP_MODULES: tuple[str, ...] = (
    LORE_MODULE, LORE_DELETE_MODULE, CHATROOM_MODULE, MODULE_WEB_SEARCH, MODULE_WEB_FETCH, MODULE_IMAGE_READ,
)
HealthCheck = Callable[[], "bool | ProbeResult"]


class ModuleConfigError(RuntimeError):
    """設定不齊（load 被要求但設定面不可用）。訊息只含閉集短碼，不含任何設定值。"""

    def __init__(self, reason_code: str = "not_configured") -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ConfigServiceHandle:
    """只驗設定的服務 handle。``available()`` 是純設定判定；``health``（選填）可做額外探活，預設不做。"""

    can_load = True
    can_unload = False

    def __init__(self, name: str, available: Callable[[], bool], *, health: HealthCheck | None = None) -> None:
        self._name = name
        self._available = available
        self._health = health

    def __repr__(self) -> str:
        return f"ConfigServiceHandle({self._name})"

    def load(self) -> None:
        if not self._available():
            raise ModuleConfigError("not_configured")

    def warm(self) -> None:
        return None

    def unload(self) -> None:
        return None  # 沒有資源要釋放；coordinator 也不會呼叫（can_unload=False）

    def probe(self) -> ProbeResult:
        if not self._available():
            return "dead"
        if self._health is None:
            return "ok"
        try:
            verdict = self._health()
        except Exception as exc:  # noqa: BLE001  只記例外類別，不記內容
            logger.warning("模組 %s health 檢查失敗：%s", self._name, type(exc).__name__)
            return "dead"
        if verdict is True:
            return "ok"
        if verdict is False:
            return "dead"
        return verdict if verdict in ("ok", "degraded", "dead") else "dead"


def _descriptor(module_id: str, *depends_on: str) -> ModuleDescriptor:
    return ModuleDescriptor(
        module_id, kind="service", depends_on=depends_on, priority="on_demand", autoload=True, needs_warm=False,
        uses_gpu=False, vram_mb=None, load_cost="none",
    )


def build_s_group_registrations(
    settings: Any, *, health: Mapping[str, HealthCheck] | None = None, transport: Any = None, resolver: Any = None,
    client_factory: Callable[[float], Any] | None = None,
) -> list[tuple[ModuleDescriptor, ModuleHandle]]:
    """S 群 ``(描述子, handle)`` 預設表；餵給 ``assemble_core(module_registrations=...)``。
    ``health`` 以 module_id 注入探活 callable（預設全無，純設定判定）。lore_delete 依賴 lore。
    ``transport``／``resolver``／``client_factory`` 與 ``with_web_image`` 同義，只是原樣交給判定函式（測試用來證明零網路）。"""
    probes = dict(health or {})
    unknown = set(probes) - set(S_GROUP_MODULES)
    if unknown:
        raise ValueError(f"health 含未知的 S 群模組：{sorted(unknown)}")
    checks: dict[str, tuple[Callable[[], bool], tuple[str, ...]]] = {
        LORE_MODULE: (lambda: LoreConfig.from_settings(settings).configured, ()),
        LORE_DELETE_MODULE: (lambda: bool(getattr(settings, "lore_delete_enabled", True)), (LORE_MODULE,)),
        CHATROOM_MODULE: (lambda: chatroom_config_from_settings(settings) is not None, ()),
        MODULE_WEB_SEARCH: (lambda: web_search_available(settings, transport=transport, resolver=resolver, client_factory=client_factory), ()),
        MODULE_WEB_FETCH: (lambda: bool(getattr(settings, "web_fetch_enabled", True)), ()),
        MODULE_IMAGE_READ: (lambda: image_read_available(settings, client_factory=client_factory), ()),
    }
    return [
        (_descriptor(module_id, *deps), ConfigServiceHandle(module_id, check, health=probes.get(module_id)))
        for module_id, (check, deps) in checks.items()
    ]
