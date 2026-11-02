"""
source_view - 下游 LLM prompt 的「來源標示」渲染

Dream 蒸餾／replay／ConceptExtractor／Verifier 等下游讀 Episode 時，不可把
user_input／agent_response 原文一視同仁餵給 LLM：agent 的未說出口內容、
工具記錄、來源未確認的舊資料都不能被當成「使用者說過」。

來源判斷只依 EvidenceUnit 的結構欄位（source_type／delivery_state／
provenance_status／content_origin），不使用 keyword／regex。

標示格式（每個 unit 一行）：
  使用者說（已確認）: ...
  使用者輸入（來源未確認）: ...                 # legacy_unknown，不得升格為使用者陳述
  助手說（已說出口）: ...
  助手回覆（來源未確認，不確定是否說出口）: ...   # legacy_unknown / delivery unknown
  助手回覆（未說出口，內容已略去）                # not_delivered／partial 進度未知：不含內容
  工具記錄（工具 X；內容出處 Y）: ...            # 內容出處不等於使用者陳述
  系統產生（reflection，非對話陳述）: ...         # reflection／dream
  （observation 一律略去：平台被動觀察不是任何人說的話）
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from echo_memory.schemas.evidence import (
    EvidenceUnit,
    can_attest_agent_said,
    can_attest_user_claim,
    has_shared_context,
    is_undelivered_agent,
)

logger = logging.getLogger("echo_memory.utils.source_view")

# 蒸餾 prompt 開頭的圖例：讓 LLM 明確區分各種來源
SOURCE_LEGEND = (
    "## 來源標示說明\n"
    "- 「使用者說（已確認）」：使用者本人確實說過的話，是使用者事實、偏好與要求的唯一可靠依據。\n"
    "- 「來源未確認」：舊資料無法確認是誰說的或是否說出口，只能當作低可信的線索，"
    "不得當作使用者確實說過。\n"
    "- 「助手說（已說出口）」：助手說過的話，只能支撐關於助手自己的知識；"
    "助手的推測、建議或猜測不是使用者的事實或偏好。\n"
    "- 「未說出口」的助手內容已被略去，不可據此推論。\n"
    "- 「工具記錄」：工具操作的記錄，內容出處依標示，不等於使用者陳述。\n"
    "不得把助手的推測、建議或工具記錄寫成使用者的事實或偏好。"
)

# support_source 值（寫入 Concept.metadata["support_source"]）
SUPPORT_USER = "user"                    # 至少一個來源有可確認的使用者陳述
SUPPORT_LEGACY = "legacy_unknown"        # 只有來源未確認的舊資料
SUPPORT_AGENT_ONLY = "agent_only"        # 只有 agent／工具／系統內容支撐
SUPPORT_RANK = {SUPPORT_AGENT_ONLY: 0, SUPPORT_LEGACY: 1, SUPPORT_USER: 2}


def classify_unit(u: EvidenceUnit) -> str:
    """回傳 unit 的來源類別。

    user_claim | user_legacy | agent_said | agent_legacy | agent_unsaid
    | tool | system | observation | redacted | shared_derived
    """
    if u.tombstoned_at is not None:
        return "redacted"
    st = u.source_type
    if st == "agent" and has_shared_context(u):
        # 帶入他人共享摘要的回覆：不得成為任何概念的來源（否則 A 的內容經 B 的對話複製進 B 的 namespace）
        return "shared_derived"
    if st == "observation":
        return "observation"
    if st == "user":
        return "user_claim" if can_attest_user_claim(u) else "user_legacy"
    if st == "agent":
        if is_undelivered_agent(u):
            return "agent_unsaid"
        if can_attest_agent_said(u):
            return "agent_said"
        return "agent_legacy"  # legacy_unknown 或 delivery_state=unknown
    if st == "tool":
        return "tool"
    return "system"  # reflection／dream


def _clip(text: str, max_chars: int | None) -> str:
    return text if max_chars is None or len(text) <= max_chars else text[:max_chars]


def render_unit(u: EvidenceUnit, *, max_chars: int | None = None) -> str | None:
    """單一 unit 的標示行；observation／已塗銷／空內容回傳 None。"""
    kind = classify_unit(u)
    if kind in ("observation", "redacted", "shared_derived"):
        return None
    if kind == "agent_unsaid":
        return "助手回覆（未說出口，內容已略去）"
    content = (u.content or "").strip()
    if not content:
        return None
    content = _clip(content, max_chars)
    if kind == "user_claim":
        return f"使用者說（已確認）: {content}"
    if kind == "user_legacy":
        return f"使用者輸入（來源未確認）: {content}"
    if kind == "agent_said":
        return f"助手說（已說出口）: {content}"
    if kind == "agent_legacy":
        return f"助手回覆（來源未確認，不確定是否說出口）: {content}"
    if kind == "tool":
        name = u.source_ref.name if u.source_ref and u.source_ref.name else "未知"
        origin = u.content_origin or "unknown"
        return f"工具記錄（工具 {name}；內容出處 {origin}）: {content}"
    return f"系統產生（{u.source_type}，非對話陳述）: {content}"


def units_for(
    episode: Any,
    units_by_episode: dict[str, list[EvidenceUnit]] | None,
) -> list[EvidenceUnit]:
    """取 Episode 的 units；map 沒有時保守合成 legacy_unknown（不寫入、不讀檔）。

    episode 有 evidence_ids 卻取不到任何 unit（證據檔缺漏）時同樣退回 legacy_unknown，
    不能讓空清單被當成「只有 agent 內容」。
    """
    if units_by_episode is not None and episode.id in units_by_episode:
        found = units_by_episode[episode.id]
        if found or not getattr(episode, "evidence_ids", None):
            return found
    from echo_memory.storage.evidence import build_legacy_units

    # 帶入過共享摘要的輪次：證據不可讀時不合成 agent unit（無從確認哪段含他人內容，寧可不用）
    shared_turn = bool((getattr(episode, "metadata", None) or {}).get("shared_context_ids"))
    return build_legacy_units(
        episode_id=episode.id,
        user_input=episode.user_input,
        agent_response="" if shared_turn else episode.agent_response,
        occurred_at=episode.timestamp,
        metadata=episode.metadata,
        via="read_time",
    )


def load_units(
    evidence_store: Any | None,
    episodes: Iterable[Any],
) -> dict[str, list[EvidenceUnit]]:
    """批次取 evidence（只讀一次 evidence.jsonl）。

    無 store 或讀取失敗時回傳空 dict，下游 units_for 會降級為 legacy_unknown（保守）。
    須在 storage_lock 外呼叫 LLM 之前或之後皆可；本函式不呼叫 LLM。
    """
    episodes = list(episodes)
    if evidence_store is None or not episodes:
        return {}
    try:
        return evidence_store.units_for_episodes(episodes)
    except Exception as e:
        logger.warning("[source_view] 讀取 evidence 失敗，降級為 legacy_unknown: %s", e)
        return {}


def render_episode_lines(
    episode: Any,
    units: Sequence[EvidenceUnit],
    *,
    max_chars: int | None = None,
) -> list[str]:
    """Episode 的標示行（user 在前、其餘依序）；無可顯示內容回傳空 list。"""
    lines = []
    for u in units:
        line = render_unit(u, max_chars=max_chars)
        if line:
            lines.append(line)
    return lines


def render_episode_block(
    index: int,
    episode: Any,
    units: Sequence[EvidenceUnit],
    *,
    max_chars: int | None = None,
) -> str:
    """`[i] 標示行...` 區塊（取代原本 使用者／助手／摘要 三行）。

    不帶 content_summary：摘要由 user＋agent 原文混合產生，會洗掉來源標示。
    """
    lines = render_episode_lines(episode, units, max_chars=max_chars)
    if not lines:
        lines = ["（無可確認內容）"]
    head = f"[{index}] "
    pad = " " * len(head)
    return head + f"\n{pad}".join(lines)


def user_only_summary(
    episode: Any,
    units: Sequence[EvidenceUnit],
    *,
    max_chars: int | None = 150,
) -> str:
    """只用 user 文字的 Episode 摘要（與共享輪次 summary 只取 user 文字的規則一致）。

    不使用 content_summary（舊資料可能混有 agent 原文）。來源未確認的 user 文字加註標示，
    助手、工具、系統內容一律不進摘要。無 user 文字時回傳空字串。
    """
    parts: list[str] = []
    for u in units:
        kind = classify_unit(u)
        content = (u.content or "").strip()
        if not content:
            continue
        if kind == "user_claim":
            parts.append(content)
        elif kind == "user_legacy":
            parts.append(f"（來源未確認）{content}")
    return _clip(" ".join(parts), max_chars)


def recent_user_summaries(
    evidence_store: Any | None,
    episodes: Iterable[Any],
    *,
    max_chars: int | None = 150,
) -> list[str]:
    """批次產生來源感知的 user-only 摘要（供 Reflection 的 recent_summaries 使用）。"""
    episodes = list(episodes)
    units_by_episode = load_units(evidence_store, episodes)
    summaries = []
    for ep in episodes:
        text = user_only_summary(ep, units_for(ep, units_by_episode), max_chars=max_chars)
        if text:
            summaries.append(text)
    return summaries


def attested_text(episode: Any, units: Sequence[EvidenceUnit]) -> str:
    """可作語意比對的文字：排除未說出口的 agent 內容；優先已說出口的 agent，其次其餘。"""
    agent = [
        u.content for u in units
        if classify_unit(u) in ("agent_said", "agent_legacy") and u.content
    ]
    if agent:
        return "\n".join(agent)
    other = [
        u.content for u in units
        if classify_unit(u) in ("user_claim", "user_legacy") and u.content
    ]
    return "\n".join(other)


def support_source(
    episodes: Iterable[Any],
    units_by_episode: dict[str, list[EvidenceUnit]] | None,
) -> str:
    """一組來源 Episode 對概念的支撐來源：user > legacy_unknown > agent_only。"""
    best = SUPPORT_AGENT_ONLY
    for ep in episodes:
        for u in units_for(ep, units_by_episode):
            kind = classify_unit(u)
            if kind == "user_claim":
                return SUPPORT_USER
            if kind == "user_legacy":
                best = SUPPORT_LEGACY
    return best
