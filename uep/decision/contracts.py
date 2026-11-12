"""決策中樞契約型別（提案 §3.2、§3.3）。葉節點：不 import ``uep`` 其他部分。

P0 的 ``DecisionPlan`` 只有 ``route`` 與 ``reply``（``reply.mode``、``interaction_to``）；其餘區段
（output、capability、provisioning、placement、model、loop、followups）P2 之後依提案 §3.3 命名擴充。
``capability`` 區段（DL-4）只放「要求收窄到哪些標籤」的提議，不放解出來的最終標籤。
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
class DecisionCapability:
    """``DecisionPlan.capability``：只是「要求收窄到哪些標籤」的提議（DL-4），不是最終標籤、也不是授權。

    ``capability_hint`` 為 None 表示不收窄（走預設表）；``()`` 表示明確要求空集合（結果走 ``capability_empty``），
    兩者語意不同、不得互換。hint 只能收窄：最終標籤由 ``uep.decision.capability.narrow_capability`` 計算，
    恆為預設表的子集。非 tuple／list（含單一字串）或含非字串元素時建構失敗（``ValueError``），區段視為無效。
    """

    capability_hint: tuple[str, ...] | None = None

    def __post_init__(self) -> None:
        hint = self.capability_hint
        if hint is None:
            return
        if not isinstance(hint, (tuple, list)):
            raise ValueError("capability_hint 必須是 tuple／list（單一字串或其他型別無效）")  # noqa: TRY004 - 區段無效統一 ValueError
        if not all(isinstance(tag, str) for tag in hint):
            raise ValueError("capability_hint 的元素必須全為字串")
        object.__setattr__(self, "capability_hint", tuple(hint))


@dataclass(frozen=True)
class DecisionChain:
    """``DecisionPlan.chain``：複合句（一句含多個獨立指示）的提議載體（Chain 契約 §3.3）。

    ``compound`` 為 None 表示 proposer 不支援該題；僅 ``turn.reply`` 的 spec 宣告 ``chain`` 區段。
    目前只是欄位增補：沒有任何消費者依此改變行為（派發由後續 PR 負責）。
    """

    compound: bool | None = None


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
    """提議的集合（不是命令）；所有區段選填，預設 None。已實作 ``route``、``reply``、``capability`` 與 consult 用的
    ``output``／``placement``／``loop``／``followups``；其餘區段（provisioning、model）之後依提案 §3.3 擴充。
    """

    route: str | None = None  # direct_chat | agent_loop | task_request | ignore
    reply: DecisionReply | None = None
    output: DecisionOutput | None = None
    placement: DecisionPlacement | None = None
    loop: DecisionLoop | None = None
    followups: tuple[DecisionFollowup, ...] | None = None
    policy: DecisionPolicy | None = None  # 僅記錄（policy.snapshot／channel.mode），不被套用
    capability: DecisionCapability | None = None  # 只收窄的 hint 提議（DL-4）；最終標籤由純函式計算
    chain: DecisionChain | None = None  # 複合句提議（Chain 契約 §3.3）；僅 turn.reply 可帶，目前無消費者


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
    policy, capability = data.get("policy"), data.get("capability")
    chain = data.get("chain")
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
        # capability_hint 的型別驗證在 DecisionCapability.__post_init__（無效即 ValueError）；None 與 [] 保持區分
        capability=_build(DecisionCapability, capability) if capability is not None else None,
        chain=_build(DecisionChain, chain) if chain is not None else None,
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
