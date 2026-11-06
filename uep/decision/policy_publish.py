"""``policy.snapshot``／``channel.mode`` 決策點（DL-P1d）：把政策快照發布與頻道模式翻轉收編進決策中樞（publish 模式）。

- 兩個 engine 都只是記錄器：驗證 facts 型別後原樣記錄，不重新計算任何值（版本、模式都由 ``HostPolicySource.publish`` 決定）。
- 取代 shadow 的不變量審計：版本連號（version_gap）、ES 與 Core 快照同版（split_version）、快照頻道模式與發布一致
  （mode_mismatch）。違反時只改 ``reason_code``，outcome 仍是 published，不影響 primary 的換版。
- ``policy.snapshot`` 每次發布都記（reason_code=cause）；``channel.mode`` 只在前後模式不同時記（reason_code=mode_flip），
  與同一次發布的快照共用同一個版本（request_id 相同）。
- 本模組不 import ``uep.echo``／``uep.turn``：以鴨子型別接 ``source.add_publish_listener``，ChannelMode 以 ``.value`` 字串傳遞。
- 初始記錄：source 建構時的快照（version 1）早於監聽者登記，所以 ``register_policy_publish`` 預設（``record_initial=True``）
  在 source 發布鎖內「掛監聽者＋取當下快照」，再補記一筆 ``cause="initial"``（``prev_version``／``prev_channel_mode`` 為 None）
  的基準：``policy.snapshot`` 的 reason_code=initial；基準的頻道模式非 None 時也會記一筆 ``channel.mode``。
  ``PolicySnapshotEngine`` 只接受 initial 作為收到的第一筆；之後出現 initial、或 prev_version 為 None 卻不是 initial，
  reason_code 改為 initial_not_first（outcome 仍是 published）。
- 順序保證（單一 drainer）：監聽者在 source 鎖外被呼叫，別的執行緒的下一版可能搶在基準送出前進來。recorder 的鎖只保護
  佇列與 drainer 旗標、絕不持鎖呼叫 hub.publish：登記時先佔住 drainer，基準排在佇列最前，其他執行緒的發布只入列後立即返回
  （不等待、不被 hub 卡住）；drainer 在鎖外依序送出佇列，佇列空時才在鎖內放掉旗標。已無 drainer 時各執行緒自己送。
  source 沒有 ``add_publish_listener_with_baseline`` 時退回 ``add_publish_listener`` 並略過基準（不保證有初始記錄）。
  監聽者跑在換版的路徑上，只做「組 request＋hub.publish」；日誌落盤沿用決策日誌的佇列與吞錯路徑，不會拖慢換版。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any

from uep.decision.contracts import (
    DecisionEngineInfo,
    DecisionPlan,
    DecisionRequest,
    DecisionResult,
)
from uep.decision.hub import DecisionHub
from uep.decision.registry import DecisionPointSpec
from uep.ids import new_id

logger = logging.getLogger("uep.decision.policy_publish")

KIND_POLICY_SNAPSHOT = "policy.snapshot"
KIND_CHANNEL_MODE = "channel.mode"
OUTCOME_PUBLISHED = "published"
OUTCOME_ACTIVE = "active"
OUTCOME_PASSIVE = "passive"
OUTCOME_INVALID = "invalid"
REASON_MODE_FLIP = "mode_flip"
REASON_VERSION_GAP = "version_gap"
REASON_SPLIT_VERSION = "split_version"
REASON_MODE_MISMATCH = "mode_mismatch"
REASON_INITIAL_NOT_FIRST = "initial_not_first"
ON_INVALID_REASON = "policy_publish_invalid"
CAUSE_INITIAL = "initial"
CAUSES = frozenset({"participants", "channel_mode", "platform", CAUSE_INITIAL})
_MODES = frozenset({OUTCOME_ACTIVE, OUTCOME_PASSIVE})
_ENGINE_ID = "policy_publish"
_ENGINE_VERSION = "dl-p1d"


def policy_snapshot_spec() -> DecisionPointSpec:
    return DecisionPointSpec(
        kind=KIND_POLICY_SNAPSHOT, mode="publish", owner="echo", outcomes=(OUTCOME_PUBLISHED,),
        budget_ms=None, on_invalid_reason=ON_INVALID_REASON, allow_native=False, shadow=False,
    )


def channel_mode_spec() -> DecisionPointSpec:
    return DecisionPointSpec(
        kind=KIND_CHANNEL_MODE, mode="publish", owner="echo", outcomes=(OUTCOME_ACTIVE, OUTCOME_PASSIVE),
        budget_ms=None, on_invalid_reason=ON_INVALID_REASON, allow_native=False, shadow=False,
    )


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_mode_or_none(value: Any) -> bool:
    return value is None or value in _MODES


def _valid_facts(facts: dict[str, Any]) -> bool:
    ttl = facts.get("ttl_s")
    return (
        all(_is_int(facts.get(k)) for k in ("version", "es_version", "core_version"))
        and (_is_int(facts.get("prev_version")) or facts.get("prev_version") is None)
        and not (facts.get("cause") == CAUSE_INITIAL and facts.get("prev_version") is not None)
        and isinstance(facts.get("platform"), str)
        and facts.get("cause") in CAUSES
        and all(_is_mode_or_none(facts.get(k)) for k in ("channel_mode", "prev_channel_mode", "core_channel_mode"))
        and (ttl is None or (isinstance(ttl, (int, float)) and not isinstance(ttl, bool)))
    )


def _result(request: DecisionRequest, outcome: str, reason: str, *, fallback: bool = False) -> DecisionResult:
    engine = (
        DecisionEngineInfo(tier="fallback", id="core.fallback", version="d80") if fallback
        else DecisionEngineInfo(tier="rule", id=_ENGINE_ID, version=_ENGINE_VERSION)
    )
    return DecisionResult(
        decision_id=new_id("dec"), request_id=request.request_id, kind=request.kind, outcome=outcome, plan=DecisionPlan(),
        confidence=0.0 if fallback else 1.0, reason_code=reason, engine=engine, fallback=fallback,
    )


class PolicySnapshotEngine:
    """記錄每次快照發布，並審計版本連號、同版與模式一致；違反只反映在 reason_code。

    帶最小狀態：是否已收過第一筆。initial（prev_version=None）只允許是第一筆，否則 reason=initial_not_first。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seen_first = False

    def decide(self, request: DecisionRequest) -> DecisionResult:
        facts = request.facts
        if not _valid_facts(facts):
            return _result(request, OUTCOME_INVALID, ON_INVALID_REASON, fallback=True)
        with self._lock:
            is_first, self._seen_first = not self._seen_first, True
        if facts["prev_version"] is None and not (is_first and facts["cause"] == CAUSE_INITIAL):
            reason = REASON_INITIAL_NOT_FIRST
        elif facts["prev_version"] is not None and facts["version"] != facts["prev_version"] + 1:
            reason = REASON_VERSION_GAP
        elif not (facts["es_version"] == facts["core_version"] == facts["version"]):
            reason = REASON_SPLIT_VERSION
        elif facts["core_channel_mode"] != facts["channel_mode"]:
            reason = REASON_MODE_MISMATCH
        else:
            reason = facts["cause"]
        return _result(request, OUTCOME_PUBLISHED, reason)


class ChannelModeEngine:
    """記錄頻道模式翻轉；outcome 為翻轉後的模式。initial 視為由「無」翻轉而來，沿用 mode_flip。"""

    def decide(self, request: DecisionRequest) -> DecisionResult:
        facts = request.facts
        if not _valid_facts(facts) or facts["channel_mode"] not in _MODES:
            return _result(request, OUTCOME_INVALID, ON_INVALID_REASON, fallback=True)
        return _result(request, facts["channel_mode"], REASON_MODE_FLIP)


def _mode_text(value: Any) -> Any:
    return getattr(value, "value", value)


def _facts_from(publication: Any) -> dict[str, Any]:
    keys = ("version", "prev_version", "platform", "cause", "ttl_s", "es_version", "core_version")
    facts: dict[str, Any] = {k: getattr(publication, k, None) for k in keys}
    for key in ("channel_mode", "prev_channel_mode", "core_channel_mode"):
        facts[key] = _mode_text(getattr(publication, key, None))
    return facts


class PolicyPublishRecorder:
    """發布監聽者：每次發布記一筆 ``policy.snapshot``，模式翻轉時再記一筆 ``channel.mode``（同版本）。

    單一 drainer：鎖只保護佇列與旗標，hub.publish 一律在鎖外；有 drainer 在送時其他執行緒只入列後返回。
    """

    def __init__(self, hub: DecisionHub, *, run_id: str | None = None) -> None:
        self._hub = hub
        self._run_id = run_id or new_id("dec")
        self._lock = threading.Lock()
        self._queue: deque[Any] = deque()
        self._draining = False

    def __call__(self, publication: Any) -> None:
        with self._lock:
            self._queue.append(publication)
            if self._draining:
                return  # 有 drainer 會依序送出，不等待
            self._draining = True
        self._drain()

    def attach(self, source: Any, *, record_initial: bool = True) -> None:
        """掛到 source；有 ``add_publish_listener_with_baseline`` 且 record_initial 時補記初始基準。

        登記前先佔住 drainer，掛上後立刻到來的發布只入列；基準排到佇列最前，由本執行緒先送。
        """
        method = getattr(source, "add_publish_listener_with_baseline", None) if record_initial else None
        if method is None:
            source.add_publish_listener(self)
            return
        with self._lock:
            self._draining = True
        try:
            baseline = method(self)
        except BaseException:
            self._drain()  # 登記失敗：放掉 drainer 並送出已入列的內容
            raise
        with self._lock:
            self._queue.appendleft(baseline)
        self._drain()

    def _drain(self) -> None:
        while True:
            with self._lock:
                if not self._queue:
                    self._draining = False
                    return
                item = self._queue.popleft()
            try:
                self._record(item)
            except Exception:
                logger.exception("政策發布記錄失敗")

    def _record(self, publication: Any) -> None:
        facts = _facts_from(publication)
        request_id = f"{self._run_id}:policy:{facts['version']}"
        snapshot = DecisionRequest(
            request_id=request_id, kind=KIND_POLICY_SNAPSHOT, trigger="event", facts=facts,
            snapshot_versions={"policy": facts["version"]} if _is_int(facts["version"]) else {},
        )
        self._hub.publish(KIND_POLICY_SNAPSHOT, snapshot)
        mode, prev = facts["channel_mode"], facts["prev_channel_mode"]
        if mode is not None and mode != prev:
            self._hub.publish(KIND_CHANNEL_MODE, DecisionRequest(
                request_id=request_id, kind=KIND_CHANNEL_MODE, trigger="event", facts=facts,
                snapshot_versions=snapshot.snapshot_versions,
            ))


def register_policy_publish(
    hub: DecisionHub, source: Any, *, run_id: str | None = None, record_initial: bool = True
) -> PolicyPublishRecorder:
    """登記兩個 publish 決策點並掛到 source 的發布監聽（鴨子型別）。

    日誌掛的是 hub 觀察者（對所有 kind 生效），與 ``log.attach`` 的先後無關；只需在要記錄的第一次 publish 之前登記。
    ``record_initial=True`` 時用 ``source.add_publish_listener_with_baseline`` 補記當下快照（cause=initial）；
    source 沒有該方法時退回 ``add_publish_listener`` 且不保證有初始記錄（不拋錯）。False 時只掛監聽者。
    """
    hub.register(policy_snapshot_spec(), PolicySnapshotEngine())
    hub.register(channel_mode_spec(), ChannelModeEngine())
    recorder = PolicyPublishRecorder(hub, run_id=run_id)
    recorder.attach(source, record_initial=record_initial)
    return recorder
