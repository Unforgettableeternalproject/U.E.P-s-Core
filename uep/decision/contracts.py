"""決策中樞契約型別（提案 §3.2、§3.3）。葉節點：不 import ``uep`` 其他部分。

P0 的 ``DecisionPlan`` 只有 ``route`` 與 ``reply``（``reply.mode``、``interaction_to``）；其餘區段
（output、capability、provisioning、placement、model、loop、followups）P2 之後依提案 §3.3 命名擴充。
所有欄位是英文列舉／ID，使用者可見字串不進 plan。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

Trigger = Literal["initial", "reeval", "event", "tick"]
EngineTier = Literal["rule", "feature_rule", "model", "llm", "fallback"]


@dataclass(frozen=True)
class DecisionRequest:
    """一次決策請求。``facts`` 為依 input_schema 的英文欄位；``native`` 為同行程原生物件。

    ``native`` 是 P0 偏離提案之處：``turn.reply`` 的 engine 吃 ``TurnCandidate``（含原文，行程內直接帶，
    ``repr=False`` 不進日誌）。契約改成跨行程前須以 ``text_refs``＋``facts`` 取代。
    只有 spec 標記 ``allow_native`` 的決策點接受 native，其他 kind 帶 native 由 ``DecisionHub.decide`` 拒絕。
    """

    request_id: str
    kind: str
    trigger: Trigger = "initial"
    parent_decision_id: str | None = None
    plan_version: int | None = None
    subject: dict[str, str] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    text_refs: tuple[str, ...] = ()
    allowed_outcomes: tuple[str, ...] = ()
    snapshot_versions: dict[str, int] = field(default_factory=dict)
    deadline: float | None = None  # time.monotonic()；sync 決策點用
    native: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class DecisionReply:
    """``DecisionPlan.reply``：沿用 Proposal 語彙（respond／passive／pass／defer）。

    ``addressed`` 為 P0 為了與 ``Proposal`` 雙向無損轉換而加的欄位（提案 §3.3 未列）。
    """

    mode: str
    interaction_to: str | None = None
    addressed: bool = True


@dataclass(frozen=True)
class DecisionEmotion:
    """``DecisionOutput.emotion``（提案 §3.3）。"""

    source: str = "none"  # none | llm_marker | preset
    preset: str | None = None


@dataclass(frozen=True)
class DecisionOutput:
    """``DecisionPlan.output``：輸出處置建議（``loop.consult(output_modality)`` 的 advice 區段）。"""

    modalities: tuple[str, ...] = ()  # text | audio_stream | typing | animation
    tts: bool = False
    emotion: DecisionEmotion = field(default_factory=DecisionEmotion)
    history: str = "commit"  # commit | ephemeral


@dataclass(frozen=True)
class DecisionPlacement:
    """``DecisionPlan.placement``：平台／executor／Presence 預配置建議。"""

    target_platform: str | None = None
    executor_ref: str | None = None
    presence_intent: str = "none"  # stay | request_move | none
    audience_scope_max: str | None = None


@dataclass(frozen=True)
class DecisionLoop:
    """``DecisionPlan.loop``：啟動範圍；``outcome`` 只用於 ``loop.consult(loop_end)`` 的 advice，start 路徑不填。"""

    start: bool = False
    trigger_kind: str | None = None
    goal_code: str | None = None
    policy_ref: str | None = None
    reeval_on: tuple[str, ...] = ()
    outcome: str | None = None  # continue | finalize


@dataclass(frozen=True)
class DecisionPolicy:
    """``DecisionPlan.policy``：政策發布事實的記錄（僅記錄，不被任何元件套用）。

    只有 ``policy.snapshot``／``channel.mode`` 可帶（spec ``plan_sections`` 宣告 ``policy``）；
    P2 之前不得新增任何讀取此區段的消費者（由 ``test_decision_policy_section_lock`` 鎖住）。
    """

    channel_mode: str | None = None  # active | passive
    snapshot_version: int | None = None


@dataclass(frozen=True)
class DecisionFollowup:
    """``DecisionPlan.followups`` 的一項：後續請求（如完成後回報）。"""

    kind: str
    after_ms: int | None = None
    on_event: str | None = None


@dataclass(frozen=True)
class Escalate:
    """consult 的升級旗標：需要超出凍結範圍的決定時只回 ``needed=True``，不回 advice（CN-1）。"""

    needed: bool = False
    reason_code: str = ""


@dataclass(frozen=True)
class DecisionPlan:
    """提議的集合（不是命令）；所有區段選填，預設 None。已實作 ``route``、``reply`` 與 consult 用的
    ``output``／``placement``／``loop``／``followups``；其餘區段（capability、provisioning、model）之後依提案 §3.3 擴充。
    """

    route: str | None = None  # direct_chat | agent_loop | task_request | ignore
    reply: DecisionReply | None = None
    output: DecisionOutput | None = None
    placement: DecisionPlacement | None = None
    loop: DecisionLoop | None = None
    followups: tuple[DecisionFollowup, ...] | None = None
    policy: DecisionPolicy | None = None  # 僅記錄（policy.snapshot／channel.mode），不被套用


def plan_to_dict(plan: DecisionPlan) -> dict[str, Any]:
    """序列化；值為 None 的區段不輸出，故舊格式（只有 route／reply）的輸出維持不變。"""
    data = asdict(plan)
    return {k: v for k, v in data.items() if v is not None}


def _build(cls: type, data: dict[str, Any], **override: Any) -> Any:
    """依 dataclass fields 過濾未知鍵後建構。"""
    names = {f.name for f in fields(cls)}
    return cls(**{**{k: v for k, v in data.items() if k in names}, **override})


def plan_from_dict(data: dict[str, Any]) -> DecisionPlan:
    """反序列化；缺少的區段為 None（相容只含 route／reply 的舊資料），頂層與區段內的未知鍵一律忽略。"""
    reply, output, placement = data.get("reply"), data.get("output"), data.get("placement")
    loop, followups = data.get("loop"), data.get("followups")
    policy = data.get("policy")
    return DecisionPlan(
        route=data.get("route"),
        reply=_build(DecisionReply, reply) if reply is not None else None,
        output=(
            _build(
                DecisionOutput, output, modalities=tuple(output.get("modalities", ())),
                emotion=_build(DecisionEmotion, output.get("emotion") or {}),
            )
            if output is not None else None
        ),
        placement=_build(DecisionPlacement, placement) if placement is not None else None,
        loop=_build(DecisionLoop, loop, reeval_on=tuple(loop.get("reeval_on", ()))) if loop is not None else None,
        followups=tuple(_build(DecisionFollowup, f) for f in followups) if followups is not None else None,
        policy=_build(DecisionPolicy, policy) if policy is not None else None,
    )


@dataclass(frozen=True)
class DecisionEngineInfo:
    tier: EngineTier
    id: str
    version: str


@dataclass(frozen=True)
class DecisionResult:
    decision_id: str
    request_id: str
    kind: str
    outcome: str
    plan: DecisionPlan | None
    confidence: float
    reason_code: str  # 安全字元集，不含原文
    engine: DecisionEngineInfo
    fallback: bool = False  # 逾時／busy／例外／驗證失敗
    plan_version: int = 1
    supersedes: str | None = None
