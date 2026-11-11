"""capability 收窄（DL-4，Capability收窄ADR）：route 預設表與 ``narrow_capability`` 純函式。

單向流：``route 預設表 ∩ known ∩ 快照標籤 ∩ hint`` -> 最終 ``capability_tags``（交給套用者組 ``LoopTrigger``）。
本模組是決策層的純資料與純函式：無 I/O、不 import 工具層／Agent 層／turn（``known`` 標籤詞彙與快照標籤集由呼叫端傳入，
詞彙由工具層 ``CAPABILITY_CEILING`` 擁有）。

授權邊界（ADR D4-5）：擴權只能走「表或快照」。``hint``、``DecisionPlan``、``LoopTrigger`` 都只能收窄：
``tags ⊆ base`` 與 ``tags ⊆ snapshot`` 恆成立，hint 沒有任何路徑新增標籤。
"""
from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from types import MappingProxyType

logger = logging.getLogger("uep.decision.capability")

CONSULT_TAG = "decision.consult"  # 與工具層 CAPABILITY_CEILING 同名；agent_loop 的 consult 能力恆保留（提案 §4.6.2）
ROUTE_AGENT_LOOP = "agent_loop"
ROUTE_DIRECT_CHAT = "direct_chat"

# 原生工具能力標籤分類（Bernie 裁決 seq 420）：唯讀類可進 direct_chat（Q-E：direct_chat 維持只讀），寫入類只進 agent_loop。
# web.search／web.fetch／image.read 讀外部內容但對本機與外部皆無寫入副作用，依 CAPABILITY_CEILING 的 internal_read 上限歸唯讀；
# 是否逐次核准屬工具層 approval_level，決策層只管標籤集合。
READ_ONLY_NATIVE_TAGS: frozenset[str] = frozenset({
    "lore.read", "chatroom.read", "web.search", "web.fetch", "image.read",
})
WRITE_NATIVE_TAGS: frozenset[str] = frozenset({"lore.write", "chatroom.post"})

# 預設表（D4-4 a：最小集合起步；原生工具標籤依裁決擴充）。擴充必須是「改程式碼＋測試＋審核」，與標籤閉集同規格。
# 表內標籤是否屬於工具層閉集，由組裝層（PR3）以 CAPABILITY_CEILING 驗證，未知標籤 = 啟動失敗。
ROUTE_DEFAULT_CAPABILITIES: Mapping[str, frozenset[str]] = MappingProxyType({
    "direct_chat": frozenset({"memory.read"}) | READ_ONLY_NATIVE_TAGS,
    "agent_loop": frozenset({"memory.read", CONSULT_TAG}) | READ_ONLY_NATIVE_TAGS | WRITE_NATIVE_TAGS,
    "task_request": frozenset(),
    "ignore": frozenset(),
})

# reason_code（決策日誌／降級事件）
REASON_CAPABILITY_EMPTY = "capability_empty"
REASON_SNAPSHOT_UNAVAILABLE = "capability_snapshot_unavailable"

# 計數鍵
COUNT_HINT_DROPPED = "decision.hint_dropped"  # hint 含表外標籤（逐標籤計）
COUNT_HINT_IGNORED_WIDER = "decision.hint_ignored_wider"  # hint 含表內但不在 base 的標籤（逐標籤計；自然被排除）
COUNT_HINT_EMPTY_MODEL = "decision.hint_empty_model"  # D4-6：模型 hint 為空集合，丟棄（視為無 hint）
COUNT_WIDEN_REJECTED = "decision.capability_widen_rejected"  # refine 試圖放寬，已限制回前版範圍
COUNT_DEGRADED = "decision.capability_degraded"  # D4-7：降級事件；另有 ``<鍵>.<reason_code>`` 細項

HINT_SOURCE_RULE = "rule"
HINT_SOURCE_MODEL = "model"


@dataclass(frozen=True)
class NarrowResult:
    """``narrow_capability`` 的輸出。``tags`` 為空代表不得啟動迴圈（agent_loop 降為 direct_chat）。"""

    tags: frozenset[str]
    route: str | None  # 套用降級後的 route：agent_loop 且 tags 為空 -> direct_chat；其餘原樣
    reason: str | None = None  # tags 為空的原因（capability_empty／capability_snapshot_unavailable）
    dropped: tuple[str, ...] = ()  # hint 中的表外標籤（已排序）
    ignored_wider: tuple[str, ...] = ()  # hint 中表內但不在 base 的標籤（已排序）
    hint_empty_model: bool = False
    widen_rejected: bool = False
    degraded: bool = False  # 降級事件：agent_loop 被降為 direct_chat，或 base 查不到（未知 route／goal_code）

    @property
    def empty(self) -> bool:
        return not self.tags

    def counters(self) -> Counter[str]:
        """本次結果對應的計數增量（不含 I/O；由 ``record_narrow`` 套到 metrics）。"""
        c: Counter[str] = Counter()
        if self.dropped:
            c[COUNT_HINT_DROPPED] += len(self.dropped)
        if self.ignored_wider:
            c[COUNT_HINT_IGNORED_WIDER] += len(self.ignored_wider)
        if self.hint_empty_model:
            c[COUNT_HINT_EMPTY_MODEL] += 1
        if self.widen_rejected:
            c[COUNT_WIDEN_REJECTED] += 1
        if self.degraded:
            c[COUNT_DEGRADED] += 1
            if self.reason:
                c[f"{COUNT_DEGRADED}.{self.reason}"] += 1
        return c


def route_base(route: str | None, table: Mapping[str, frozenset[str]] = ROUTE_DEFAULT_CAPABILITIES) -> frozenset[str] | None:
    """route -> 預設標籤；未知 route 回 None（呼叫端交給 ``narrow_capability`` 走 fail-closed）。"""
    if not isinstance(route, str):
        return None
    base = table.get(route)
    return frozenset(base) if base is not None else None


def _as_tag_set(value: object) -> frozenset[str] | None:
    """合法的標籤集合（set／frozenset，元素全為字串）-> frozenset；其餘回 None。"""
    if isinstance(value, (set, frozenset)) and all(isinstance(t, str) for t in value):
        return frozenset(value)
    return None


def narrow_capability(
    base: Collection[str] | None,
    hint: object,
    snapshot_tags: Collection[str] | None,
    known: Collection[str],
    *,
    route: str | None = None,
    hint_source: str = HINT_SOURCE_RULE,
    previous_tags: Collection[str] | None = None,
) -> NarrowResult:
    """計算最終 capability 標籤（純函式，fail-closed）。

    - ``base``：``route_base(route)`` 或 ``capability_lookup(goal_code)``；None（查不到）視為空集合。
    - ``hint``：``DecisionCapability.capability_hint``；None = 不收窄，``()`` = 要求空集合。型別錯誤（含單一字串）
      一律視為 ``()``（fail-closed）。``hint_source="model"`` 時空 hint 丟棄並計數（D4-6），視為無 hint。
    - ``snapshot_tags``：政策快照提供的可用標籤；None 或形狀錯誤 -> 空集合（``capability_snapshot_unavailable``）。
    - ``known``：工具層閉集詞彙；表外標籤丟棄。
    - ``previous_tags``：``plan.refine`` 前版的結果；新結果超出前版的部分被限制掉（單調，與 AL-3 同構）。

    保證：``tags ⊆ base``、``tags ⊆ snapshot_tags``、``tags ⊆ known``；``agent_loop`` 的 ``decision.consult``
    不會被 hint 移除（但仍須在 base 與快照內）；只剩 consult 視為空。
    """
    known_set = frozenset(known)
    base_set = _as_tag_set(base) if base is not None else None
    snap = _as_tag_set(snapshot_tags) if snapshot_tags is not None else None
    reason: str | None = None
    if snap is None:
        reason = REASON_SNAPSHOT_UNAVAILABLE
    elif base_set is None:
        reason = REASON_CAPABILITY_EMPTY
    base_missing = base_set is None
    tags = (base_set or frozenset()) & known_set & (snap or frozenset())

    dropped: tuple[str, ...] = ()
    ignored_wider: tuple[str, ...] = ()
    hint_empty_model = False
    hint_tags: frozenset[str] | None = None  # None = 不收窄
    if hint is not None:
        if not isinstance(hint, (tuple, list, frozenset, set)) or not all(isinstance(t, str) for t in hint):
            hint_tags = frozenset()  # 型別錯誤：fail-closed，等同要求空集合
        elif not hint and hint_source == HINT_SOURCE_MODEL:
            hint_empty_model = True  # D4-6：模型輸出 () 多半是失誤，丟棄（視為無 hint）而非降級
        else:
            hint_tags = frozenset(hint)
    if hint_tags is not None:
        dropped = tuple(sorted(hint_tags - known_set))
        ignored_wider = tuple(sorted((hint_tags & known_set) - (base_set or frozenset())))
        keep = hint_tags & known_set
        if route == ROUTE_AGENT_LOOP:
            keep = keep | {CONSULT_TAG}
        tags &= keep

    widen_rejected = False
    if previous_tags is not None:
        prev = _as_tag_set(previous_tags) or frozenset()
        if tags - prev:
            widen_rejected = True
            tags &= prev  # 以交集限制：既不超過前版，也不超過當前表與快照

    if tags <= {CONSULT_TAG}:
        tags = frozenset()  # 空集合，或只剩 consult（單獨存在沒有意義）
        reason = reason or REASON_CAPABILITY_EMPTY

    out_route = ROUTE_DIRECT_CHAT if (route == ROUTE_AGENT_LOOP and not tags) else route
    degraded = not tags and (route == ROUTE_AGENT_LOOP or base_missing)
    return NarrowResult(
        tags=frozenset(tags), route=out_route, reason=reason, dropped=dropped, ignored_wider=ignored_wider,
        hint_empty_model=hint_empty_model, widen_rejected=widen_rejected, degraded=degraded,
    )


def record_narrow(result: NarrowResult, metrics: Counter[str], *, ref: str | None = None) -> None:
    """把 ``counters()`` 套到呼叫端的 metrics，降級事件另記一行 log（reason_code 與 route，不含 hint 內容）。"""
    metrics.update(result.counters())
    if result.degraded:
        logger.info("capability 降級：reason_code=%s route=%s ref=%s", result.reason, result.route, ref)
