"""ThinkStage 的長對話壓縮：把 ``echo_thought_core.ContextManager`` 接進串流歷史。

## 為什麼需要轉接

ContextManager 吃的是 pydantic ``Message``，壓縮用的是 backend 的 ``summarize`` / ``compact``
（一次性、非串流）；它完全不認得 ``stream_query``。而 Think 的歷史是 ``list[dict]``，
而且同一批 dict 物件被 ``_assistant_by_turn`` / ``_user_by_turn`` 以**身分**索引
（``settle`` / ``retract`` 靠它改寫、移除）。所以這裡不讓 ContextManager 擁有歷史，
而是每次壓縮時把歷史快照載入、跑完再依 ``hidx`` 對回原 dict：
串流用的 backend 與壓縮用的 backend 是兩個物件，互不影響。

## 四層

- 60% 微清理（文字對話幾乎無事可做）、80% 增量摘要（最舊 10 則）、92% 完整壓縮；
- 第四層是 413（context 過長）時的緊急截斷，由 ``LLMThinkStage._tokens`` 在請求失敗時呼叫 :meth:`emergency`。

## 不變條件

- **背景執行、原子替換**：``compress`` 在背景 task 內 await LLM，期間歷史照常增長；
  回來後以單一 ``history[:] = ...`` 替換（同步、無 await），並把壓縮期間新增的項目接在後面、
  已被 ``retract`` / ``settle`` 移除的項目剔除；``reset_context`` 之後的結果整個作廢。
  ⚠️ 絕不「先清空原始內容再等 LLM 回摘要」（Claude Code #40352、Codex #13946 的坑）。
- **Sacred 人設**：人設就是 ``system_prompt``，每次請求都由 ``system_instruction`` 單獨帶，
  從不進歷史、所以不會被壓縮；另外把它放進 ContextManager 的 sacred，讓第三層摘要提示逐字帶著它。
  第四層會注入的 ``Emergency Recovery`` 人設訊息在這裡丟棄（人設已在 system，再放進歷史只是重複）。
- 最近 ``keep_recent`` 則原文永遠保留（第三層預設會把全部壓成單一摘要，這裡把尾端補回）。

這個模組屬於 ``uep/stream/adapters/``，可以 import ``echo_thought_core``。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_COMPRESS_MODEL = "gpt-5.6-luna"
"""壓縮用模型預設值（走 openai 後端）；可由 ``UEP_LLM_COMPRESS_MODEL`` 覆寫。"""

_HIDX = "hidx"
"""載入 ContextManager 時掛在 Message.metadata 的原歷史索引。"""

_MAX_TOKENS_PER_CHAR = 4
"""預檢用的上界：BPE 對單一字元最多約 4 token（UTF-8 位元組數上限），
字元數 × 4 都不到 60% 預算就必定不需要壓縮，可略過 tokenizer。"""

_OVERFLOW_RE = re.compile(
    r"context_length_exceeded|maximum context length|request too large|payload too large|"
    r"context window",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CompressConfig:
    """長對話壓縮設定；由 ``UepSettings.compress_config()`` 解析後注入，adapter 本身不讀環境變數。"""

    enabled: bool = True
    model: str = DEFAULT_COMPRESS_MODEL
    context_window: int = 128_000
    """用來算 60／80／92% 的 token 預算（不是模型的硬上限，留餘裕給 system、記憶注入與輸出）。"""
    reasoning_effort: str = "low"
    max_output_tokens: int = 4096
    cooldown_seconds: float = 30.0
    """兩次成功壓縮之間的最小間隔（ContextManager 的 ``compaction_cooldown_seconds``）。"""
    failure_backoff_seconds: float = 60.0
    """壓縮失敗或無效後，多久內不再嘗試（避免每輪都打一次失敗的 LLM 呼叫）。"""
    keep_recent: int = 4
    """永遠保留原文的最近訊息數；第四層也只保留這麼多。"""


@dataclass
class CompressOutcome:
    """一次壓縮嘗試的結果。"""

    tier: int
    changed: bool
    tokens_before: int
    tokens_after: int


def is_context_overflow(exc: BaseException) -> bool:
    """例外是否代表「請求的 context 過長」：HTTP 413，或 OpenAI 的 ``context_length_exceeded``。

    用形狀判斷（``status_code`` / ``code`` / 訊息），不 import openai 的例外型別。
    """
    if getattr(exc, "status_code", None) == 413:
        return True
    code = getattr(exc, "code", None)
    body = getattr(exc, "body", None)
    if code is None and isinstance(body, dict):
        code = body.get("code") or (body.get("error") or {}).get("code")
    if code == "context_length_exceeded":
        return True
    return bool(_OVERFLOW_RE.search(str(exc)))


def build_compress_backend(config: CompressConfig, think: Any) -> Any:
    """以 Core 現有的 openai 後端建壓縮用 backend（沿用 ``think`` 的金鑰／base_url，只換模型）。

    ``think`` 是 :class:`~uep.stream.adapters.think_config.ThinkConfig`。缺金鑰時由
    ``require_credentials`` 拋 ``ThinkConfigError``，呼叫端自行決定降級。
    """
    import dataclasses

    from echo_thought_core.backends.registry import BackendRegistry

    cfg = dataclasses.replace(think, provider="openai", model=config.model, api="chat")
    cfg.require_credentials()
    opts = cfg.backend_options()
    opts["reasoning_effort"] = config.reasoning_effort
    opts["max_output_tokens"] = config.max_output_tokens
    return BackendRegistry.default().create("openai", opts)


class HistoryCompressor:
    """持有一個 ContextManager，對 Think 的 ``list[dict]`` 歷史做壓縮。"""

    def __init__(
        self,
        backend: Any,
        config: CompressConfig,
        *,
        system_text: Callable[[], str | None] = lambda: "",
    ) -> None:
        from echo_thought_core.context_manager import ContextManager
        from echo_thought_core.schemas.config import CompactionConfig

        self.config = config
        self._system_text = system_text
        self._manager = ContextManager(
            config=CompactionConfig(
                backend="openai",
                model=config.model,
                compaction_model=config.model,
                context_window_size=config.context_window,
                compaction_cooldown_seconds=config.cooldown_seconds,
                max_emergency_messages=max(1, config.keep_recent),
            ),
            backend=backend,
        )

    # --- 對外 ---

    def might_need(self, history: list[dict[str, str]]) -> bool:
        """便宜的預檢：歷史字元數遠低於 60% 預算就直接略過（不跑 tokenizer）。"""
        chars = sum(len(h.get("content") or "") + 4 for h in history) + len(self._system_text() or "")
        return chars * _MAX_TOKENS_PER_CHAR >= int(self.config.context_window * 0.6)

    def reset(self) -> None:
        """``reset_context`` 時呼叫：清掉 ContextManager 的冷卻、反抖動與緊急斷路器。"""
        self._manager.reset_session()

    async def compress(
        self,
        history: list[dict[str, str]],
        *,
        is_stale: Callable[[], bool] = lambda: False,
    ) -> CompressOutcome | None:
        """依用量跑第一至三層；達標且有變化時原子替換 ``history``。

        回傳 ``None`` = 用量未達 60%（沒做事）；``changed=False`` = 達標但沒壓成
        （冷卻、反抖動、LLM 失敗降級），呼叫端據此退避。``is_stale`` 為真（脈絡已被
        ``reset_context``）時丟棄結果。
        """
        from echo_thought_core.schemas.session import CompressionTier

        snapshot = list(history)
        if len(snapshot) <= self.config.keep_recent:
            return None
        manager = self._load(snapshot)
        tier = manager.state.current_tier
        if tier == CompressionTier.TIER_0_NORMAL:
            return None
        before = manager.history.total_tokens

        await manager.get_messages_async()  # 依用量分層壓縮；L3 在此以 session rotate 實作
        compressed = list(manager.history.messages)
        if all(m.metadata.get(_HIDX) is not None for m in compressed):
            return CompressOutcome(int(tier), False, before, before)
        if is_stale():
            logger.info("壓縮結果丟棄：脈絡已在壓縮期間重置")
            return CompressOutcome(int(tier), False, before, before)

        merged, fresh = self._map_back(snapshot, compressed)
        merged = self._with_recent_tail(snapshot, merged)
        # ↓ 以下全部同步、無 await：與其他協程的歷史讀寫之間是原子的
        alive = {id(h) for h in history}
        snapshot_ids = {id(h) for h in snapshot}
        kept = [m for m in merged if id(m) in alive or id(m) in fresh]
        added_meanwhile = [h for h in history if id(h) not in snapshot_ids]
        history[:] = kept + added_meanwhile
        return CompressOutcome(
            int(tier), True, before, sum(m.token_count or 0 for m in manager.history.messages)
        )

    def emergency(self, history: list[dict[str, str]]) -> bool:
        """第四層：收到 413 後同步緊急截斷，只留最近 ``keep_recent`` 則。回傳是否真的縮短了歷史。

        斷路器由呼叫端（每個請求最多重試一次）負責，所以每次先重置 ContextManager 的單次嘗試守衛。
        """
        snapshot = list(history)
        if len(snapshot) <= self.config.keep_recent:
            return False
        manager = self._load(snapshot)
        manager.emergency.reset()
        result = manager.handle_api_error(413)
        if result is None or result.tokens_saved <= 0:
            return False
        kept = [
            snapshot[m.metadata[_HIDX]]
            for m in manager.history.messages
            if m.metadata.get(_HIDX) is not None  # 丟掉 Emergency Recovery 人設訊息：人設已在 system
        ]
        start = 0
        while start < len(kept) - 1 and kept[start].get("role") != "user":
            start += 1  # 不以孤兒 assistant 開頭
        kept = kept[start:]
        if not kept or len(kept) >= len(snapshot):
            return False
        history[:] = kept
        return True

    # --- 內部 ---

    def _load(self, snapshot: list[dict[str, str]]) -> Any:
        """把歷史快照載入 ContextManager（重算 token、同步 system 與 sacred），回傳該 manager。"""
        manager = self._manager
        system = self._system_text() or ""
        manager.history.messages.clear()
        manager.history.total_tokens = 0
        for index, item in enumerate(snapshot):
            manager.add_message(
                role=item.get("role") or "user",
                content=item.get("content") or "",
                metadata={_HIDX: index},
            )
        manager.sacred.get_sacred_content().persona_context = system or None
        manager.budget.update(system=manager.counter.count(system))
        manager.state.current_tokens = manager.budget.total_tokens
        manager.state.current_tier = manager.budget.check_tier()
        return manager

    @staticmethod
    def _map_back(snapshot: list[dict[str, str]], compressed: list[Any]) -> tuple[list[dict[str, str]], set[int]]:
        """壓縮後的 Message 對回 dict：帶 ``hidx`` 的還原成原物件，其餘（摘要）新建；回傳 (列表, 新建 id 集合)。"""
        out: list[dict[str, str]] = []
        fresh: set[int] = set()
        for msg in compressed:
            idx = msg.metadata.get(_HIDX)
            if idx is not None:
                out.append(snapshot[idx])
            elif not msg.metadata.get("emergency_recovery"):
                item = {"role": msg.role, "content": msg.content}
                fresh.add(id(item))
                out.append(item)
        return out, fresh

    def _with_recent_tail(self, snapshot: list[dict[str, str]], merged: list[dict[str, str]]) -> list[dict[str, str]]:
        """確保最近 ``keep_recent`` 則原文仍在（第三層會把全部壓成單一摘要）；缺的依序補在尾端。"""
        n = len(snapshot)
        start = max(0, n - self.config.keep_recent)
        while 0 < start and snapshot[start].get("role") != "user":
            start -= 1  # 尾端從 user 開始，避免孤兒 assistant
        present = {id(m) for m in merged}
        return merged + [h for h in snapshot[start:] if id(h) not in present]


__all__ = [
    "DEFAULT_COMPRESS_MODEL",
    "CompressConfig",
    "CompressOutcome",
    "HistoryCompressor",
    "build_compress_backend",
    "is_context_overflow",
]
