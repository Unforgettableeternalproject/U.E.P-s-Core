"""模組狀態、readiness 對照、轉移表與事件 payload 契約（Module Coordinator 設計稿 §6、§8；D108）。

純資料與純函式，無 runtime 行為。本檔不得 import ``uep.tools``／``uep.decision``／``uep.echo``／``uep.stream``
或任何子系統（由 ``tests/uep/test_import_boundary.py`` 強制）。

R1：patch／reload 只留掛點（``ready -> loading``），v1 不啟用。R3：v1 只管服務／設定閘，本地模型只觀測。
"""
from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Literal, get_args

from uep.contracts.provision import PROVISION_ACTIONS, PROVISION_OUTCOMES

# --- 事件名（R2：取代暫定的三個 module.* 事件名） ---
MODULE_STATE_CHANGED = "module.state_changed"
MODULE_PROVISION_RESOLVED = "module.provision_resolved"

ModuleState = Literal["unloaded", "loading", "warming", "ready", "degraded", "unloading", "failed"]
Readiness = Literal["ready", "warming", "unavailable"]
ModuleCause = Literal[
    "proposal", "demand", "health", "eviction", "pressure", "idle", "operator", "shutdown", "resync", "startup",
]

MODULE_STATES: frozenset[str] = frozenset(get_args(ModuleState))
READINESS_VALUES: frozenset[str] = frozenset(get_args(Readiness))
MODULE_CAUSES: frozenset[str] = frozenset(get_args(ModuleCause))

# 7 態 -> 3 態 readiness（由 coordinator 計算並放進事件；註冊表不重新推導）。
# degraded 事件同時帶 state=degraded；degradable 的消費端行為屬 R5（待裁），此處只給 readiness。
READINESS_OF_STATE: Mapping[str, Readiness] = MappingProxyType({
    "unloaded": "unavailable",
    "loading": "warming",
    "warming": "warming",
    "ready": "ready",
    "degraded": "ready",
    "unloading": "unavailable",
    "failed": "unavailable",
})

# 合法轉移（§6.1、D109）。``ready -> loading`` 是 patch／reload 掛點（R1，v1 不啟用）；任一態 -> failed（未預期例外）。
# D109：新增 ``loading／warming／failed -> unloading``（取消載入中的操作、清理 failed 殘留）。原操作未結束時只登記取消意圖，
# 不並行 unload；逾時轉 failed 並保留占用；舊操作的晚完成不得推進到 ready；確認清理完才進 unloaded。
_BASE_TRANSITIONS: dict[str, frozenset[str]] = {
    "unloaded": frozenset({"loading"}),
    "loading": frozenset({"warming", "ready", "unloading"}),  # 無需暖機者直接 ready
    "warming": frozenset({"ready", "unloading"}),
    "ready": frozenset({"degraded", "unloading", "loading"}),
    "degraded": frozenset({"ready", "unloading"}),
    "unloading": frozenset({"unloaded"}),
    "failed": frozenset({"loading", "unloading"}),
}
TRANSITIONS: Mapping[str, frozenset[str]] = MappingProxyType({
    src: dsts | ({"failed"} if src != "failed" else frozenset()) for src, dsts in _BASE_TRANSITIONS.items()
})

# 事件 payload 白名單（不含任何原文與路徑；reason_code 為閉集短碼）
STATE_CHANGED_KEYS: tuple[str, ...] = (
    "module_id", "revision", "epoch", "state", "prev_state", "readiness", "reason_code", "cause", "decision_id", "attempt",
)
PROVISION_RESOLVED_KEYS: tuple[str, ...] = (
    "module_id", "decision_id", "plan_version", "action", "outcome", "reason_code",
)
# 這些狀態的事件必須帶 reason_code
_REASON_REQUIRED_STATES = frozenset({"failed", "degraded", "unloaded", "unloading"})


def readiness_of(state: str) -> Readiness:
    """七態對應的 readiness；未知狀態拋 ``ValueError``。"""
    try:
        return READINESS_OF_STATE[state]
    except KeyError:
        raise ValueError(f"未知的模組狀態：{state!r}") from None


def is_legal_transition(src: str, dst: str) -> bool:
    """``src -> dst`` 是否為合法轉移（自我轉移與未知狀態一律非法）。"""
    return dst in TRANSITIONS.get(src, frozenset())


# 同態快照（prev_state == state）不經轉移表，走獨立發布路徑；一般同態轉移仍非法（D109）
SNAPSHOT_CAUSES: frozenset[str] = frozenset({"startup", "resync"})

# reload（``ready -> loading``）掛點保留，runtime 一律拒絕啟用（R1）
RELOAD_ENABLED = False


def is_legal_publish(prev: str | None, state: str, cause: str | None) -> bool:
    """coordinator 發布前的轉移檢查：同態只有 startup／resync 快照合法；``prev`` 為 None（首次快照）只允許快照原因；
    其餘依轉移表。reload 掛點（ready -> loading）在 ``RELOAD_ENABLED`` 為 False 時拒絕。"""
    if state not in MODULE_STATES:
        return False
    if prev is None:
        return cause in SNAPSHOT_CAUSES
    if prev == state:
        return cause in SNAPSHOT_CAUSES
    if prev == "ready" and state == "loading" and not RELOAD_ENABLED:
        return False
    return is_legal_transition(prev, state)


def unavailable_payload(
    module_id: str, *, reason_code: str, revision: int = 1, epoch: int = 0, state: str = "unloaded", cause: str = "startup",
) -> dict[str, Any]:
    """組出 readiness=unavailable 的 ``module.state_changed`` payload（已通過 ``validate_state_changed_payload``）。
    供尚無 coordinator 時的靜態不可用標記（例如 lore 未設定）使用。"""
    payload: dict[str, Any] = {
        "module_id": module_id, "revision": revision, "epoch": epoch, "state": state,
        "readiness": readiness_of(state), "reason_code": reason_code, "cause": cause,
    }
    if payload["readiness"] != "unavailable":
        raise ValueError("unavailable_payload 的 state 必須對應 unavailable")
    validate_state_changed_payload(payload)
    return payload


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_state_changed_payload(payload: Mapping[str, Any]) -> None:
    """驗證 ``module.state_changed`` payload；不合規拋 ``ValueError``（訊息不含 payload 內容）。

    必填：module_id（非空字串）、revision／epoch（非負整數）、state、readiness（須與 state 對照一致）。
    選填：prev_state、cause、decision_id、attempt、reason_code（failed／degraded／unloaded／unloading 必填）。
    不得帶白名單以外的鍵。"""
    extra = set(payload) - set(STATE_CHANGED_KEYS)
    if extra:
        raise ValueError(f"payload 含白名單以外的鍵：{sorted(extra)}")
    module = payload.get("module_id")
    if not isinstance(module, str) or not module:
        raise ValueError("module_id 必須是非空字串")
    for key in ("revision", "epoch"):
        value = payload.get(key)
        if not _is_int(value) or value < 0:
            raise ValueError(f"{key} 必須是非負整數")
    state = payload.get("state")
    if state not in MODULE_STATES:
        raise ValueError("state 不是合法的模組狀態")
    if payload.get("readiness") != READINESS_OF_STATE[state]:
        raise ValueError("readiness 與 state 的對照不一致")
    prev = payload.get("prev_state")
    if prev is not None and prev not in MODULE_STATES:
        raise ValueError("prev_state 不是合法的模組狀態")
    cause = payload.get("cause")
    if cause is not None and cause not in MODULE_CAUSES:
        raise ValueError("cause 不是合法的原因")
    for key in ("reason_code", "decision_id"):
        value = payload.get(key)
        if value is not None and (not isinstance(value, str) or not value):
            raise ValueError(f"{key} 必須是非空字串")
    attempt = payload.get("attempt")
    if attempt is not None and (not _is_int(attempt) or attempt < 1):
        raise ValueError("attempt 必須是正整數")
    if state in _REASON_REQUIRED_STATES and not payload.get("reason_code"):
        raise ValueError(f"state={state} 必須帶 reason_code")


def validate_provision_resolved_payload(payload: Mapping[str, Any]) -> None:
    """驗證 ``module.provision_resolved`` payload；不合規拋 ``ValueError``。"""
    extra = set(payload) - set(PROVISION_RESOLVED_KEYS)
    if extra:
        raise ValueError(f"payload 含白名單以外的鍵：{sorted(extra)}")
    for key in ("module_id", "decision_id"):
        value = payload.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{key} 必須是非空字串")
    plan_version = payload.get("plan_version")
    if not _is_int(plan_version) or plan_version < 0:
        raise ValueError("plan_version 必須是非負整數")
    if payload.get("action") not in PROVISION_ACTIONS:
        raise ValueError("action 不是合法的提議動作")
    outcome = payload.get("outcome")
    if outcome not in PROVISION_OUTCOMES:
        raise ValueError("outcome 不是合法的處置結果")
    reason = payload.get("reason_code")
    if reason is not None and (not isinstance(reason, str) or not reason):
        raise ValueError("reason_code 必須是非空字串")
    if outcome in {"deferred", "rejected", "dropped"} and not reason:
        raise ValueError(f"outcome={outcome} 必須帶 reason_code")
