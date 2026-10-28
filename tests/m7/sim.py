"""
M7 整合前驗收的測試輔助：模擬 Echo Stream 的 EchoMemoryAdapter 呼叫形狀（非測試檔）。

Echo Stream（Echo-Stream/echo_stream/adapters/memory_echo.py）是唯一 import echo_memory 的宿主端，
它對引擎的實際呼叫為：
  * retrieve：engine.retrieve(text, top_k, min_similarity, exclude_session_id) + 相似度二次門檻
  * store：evidence 以 dict 傳入 -> _coerce_evidence -> EvidenceInput；
           engine.store_episode(user_input=, agent_response=, session_id=, salience…, evidence=,
           idempotency_key=)；失敗只記錄、回 None（旁路）
  * dream：engine.dream(triggered_by)
  * 全部在 executor 執行緒內呼叫（跨執行緒）

這裡用同步版本重現同一形狀（不 import echo_stream：它的 config 會去找 echo_memory repo 路徑），
所有 LLM 與 embedding 皆為 mock，不連外。
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput, SourceRef, TextSpan

logger = logging.getLogger(__name__)

DM = {"channel_kind": "dm", "audience_size": 1}


def coerce_evidence(item: Any) -> EvidenceInput:
    """與 Echo Stream ``_coerce_evidence`` 同一份轉換（dict -> EvidenceInput）。"""
    if isinstance(item, EvidenceInput):
        return item
    data = dict(item)
    ref = data.get("source_ref")
    if isinstance(ref, dict):
        ref = dict(ref)
        ref["ref_ids"] = tuple(ref.get("ref_ids") or ())
        data["source_ref"] = SourceRef(**ref)
    span = data.get("text_span")
    if isinstance(span, dict):
        data["text_span"] = TextSpan(**span)
    for key in ("origin_evidence_ids", "derived_from"):
        if key in data:
            data[key] = tuple(data[key] or ())
    return EvidenceInput(**data)


def user_ev(text: str, speaker: str = "u1", **kw: Any) -> dict[str, Any]:
    attrs = kw.pop("attrs", dict(DM))
    return {"source_type": "user", "content": text, "speaker_id": speaker, "attrs": attrs, **kw}


def agent_ev(text: str, **kw: Any) -> dict[str, Any]:
    return {"source_type": "agent", "content": text, "speaker_id": "agent", **kw}


class RoutedLLM:
    """依 prompt 特徵分派的假 LLM；每個 route 可為 str、callable(prompt)->str。未命中回 "[]"。"""

    MARKERS = {
        "distill": "知識蒸餾專家",
        "replay": "記憶反思專家",
        "evidence_review": "證據審核員",
        "contradiction": "知識一致性檢驗員",
        "hypothesis": "假說審核員",
        "corroborate": "概念變更驗證器",
        "share": "分享審核員",
        "reflect": "記憶系統的反思模組",
    }

    def __init__(self, **routes: Any) -> None:
        self.routes: dict[str, Any] = dict(routes)
        self.calls: list[tuple[str, str]] = []

    def prompts(self, kind: str) -> list[str]:
        return [p for k, p in self.calls if k == kind]

    def __call__(self, prompt: str) -> str:
        kind = next((k for k, m in self.MARKERS.items() if m in prompt), "other")
        self.calls.append((kind, prompt))
        route = self.routes.get(kind, "[]")
        out = route(prompt) if callable(route) else route
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)


class EchoStreamSim:
    """EchoMemoryAdapter 的同步重現（含跨執行緒呼叫）。"""

    def __init__(self, engine: MemoryEngine, session_id: str = "es-session") -> None:
        self.engine = engine
        self.session_id = session_id
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="echo-memory-sim")
        self.last_retrieve_degraded = False

    def close(self) -> None:
        self._pool.shutdown(wait=True)

    def _run(self, fn: Callable[[], Any]) -> Any:
        return self._pool.submit(fn).result()

    def retrieve(self, text: str, *, top_k: int = 3, min_match: float = 0.0) -> str | None:
        if not text or not text.strip():
            return None

        def _work() -> str | None:
            ctx = self.engine.retrieve(
                text, top_k=top_k, min_similarity=min_match,
                exclude_session_id=self.session_id,
            )
            self.last_retrieve_degraded = getattr(ctx, "degraded", False) is True
            return None if ctx.is_empty() else ctx.to_text()

        return self._run(_work)

    def store(
        self,
        user_text: str,
        spoken_text: str,
        *,
        session_id: str | None = None,
        idempotency_key: str | None = None,
        evidence: list[Any] | None = None,
        salience: float = 0.5,
        **metadata: Any,
    ) -> dict[str, Any] | None:
        """旁路語意：任何失敗只記錄、回 None。"""
        if not user_text or not user_text.strip():
            return None
        extra: dict[str, Any] = dict(metadata)
        try:
            if evidence:
                extra["evidence"] = [coerce_evidence(e) for e in evidence]
            if idempotency_key is not None:
                extra["idempotency_key"] = idempotency_key

            def _write() -> str:
                return self.engine.store_episode(
                    user_input=user_text, agent_response=spoken_text,
                    session_id=session_id or self.session_id, salience=salience, **extra,
                )

            episode_id = self._run(_write)
            units = self._run(lambda: list(self.engine.evidence.get_by_episode(episode_id)))
            return {
                "episode_id": episode_id,
                "unit_ids": [u.id for u in units if u.source_type == "user"],
                "all_unit_ids": [u.id for u in units],
            }
        except Exception as exc:  # noqa: BLE001 - 與 adapter 相同：旁路
            logger.warning("sim store 失敗（旁路）：%s: %s", type(exc).__name__, exc)
            return None

    def dream(self, triggered_by: str = "manual") -> dict[str, Any]:
        return self._run(lambda: self.engine.dream(triggered_by))
