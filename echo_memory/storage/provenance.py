"""
Provenance Resolver - 沿 Concept 衍生鏈回溯原始 Evidence

回溯規則：
  - Concept.derivations 的 parent 可為 evidence / episode / concept。
  - concept parent 依 parent_version 綁定：只採計該版本當時已存在的 derivation
    （at_version <= parent_version），parent 之後的修改不會改寫歷史。
  - episode parent 經 Episode.evidence_ids 解析；舊 Episode 以合成的 legacy_unknown unit 代替。
  - 沒有 derivations 的舊 Concept，以 source_episode_ids 視為 legacy 來源（所有版本皆可見）。
  - 循環依賴：偵測後中止該分支並回報 cycle，不崩潰。
  - 來源不存在：記為 missing，其餘分支照常解析。

純結構解析，不讀取文字內容推測來源。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from echo_memory.schemas.evidence import EvidenceUnit, can_attest_user_claim
from echo_memory.schemas.provenance import (
    CycleReport,
    Derivation,
    IndependentSupport,
    MissingRef,
    PathStep,
    ProvenancePath,
    ProvenanceResult,
)

logger = logging.getLogger("echo_memory.provenance")

DEFAULT_MAX_DEPTH = 32


@dataclass
class _Ctx:
    """單次解析的可變狀態。"""

    result: ProvenanceResult
    evidence: dict[str, EvidenceUnit] = field(default_factory=dict)
    paths: dict[str, ProvenancePath] = field(default_factory=dict)
    expanded: set[tuple[str, int | None]] = field(default_factory=set)
    episode_units: dict[str, tuple[Any, list[EvidenceUnit]] | None] = field(default_factory=dict)
    cycle_keys: set[tuple[str, ...]] = field(default_factory=set)
    missing_keys: set[tuple[str, str, tuple[str, ...]]] = field(default_factory=set)


class ProvenanceResolver:
    """解析 Concept 的多階來源。

    Args:
        semantic: SemanticMemory（需有 get(concept_id)）
        get_episode: episode_id -> Episode | None
        evidence: EvidenceStore（需有 get(ids) 與 units_for_episode(episode)）
        max_depth: concept 衍生鏈深度上限，超過則中止並標 truncated
    """

    def __init__(
        self,
        semantic: Any,
        get_episode: Callable[[str], Any],
        evidence: Any,
        max_depth: int = DEFAULT_MAX_DEPTH,
    ):
        self._semantic = semantic
        self._get_episode = get_episode
        self._evidence = evidence
        self._max_depth = max_depth

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 公開 API
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    def resolve(self, concept_id: str, at_version: int | None = None) -> ProvenanceResult:
        """回溯 concept_id（預設目前版本；可指定 at_version 重現當時的來源）到原始 Evidence。"""
        concept = self._semantic.get(concept_id)
        if concept is None:
            return ProvenanceResult(
                concept_id=concept_id, bound_version=at_version,
                current_version=None, found=False,
            )
        bound = at_version if at_version is not None else concept.version
        result = ProvenanceResult(
            concept_id=concept_id, bound_version=bound, current_version=concept.version,
        )
        ctx = _Ctx(result=result)
        self._expand_concept(
            concept, bound, stack=[concept_id],
            steps=[PathStep("concept", concept_id, bound)], ctx=ctx,
        )
        result.evidence = list(ctx.evidence.values())
        result.paths = [ctx.paths[i] for i in ctx.evidence]
        return result

    def independent_support(self, concept_ids: Sequence[str] | Iterable[str]) -> IndependentSupport:
        """以「原始 Evidence」去重，計算一組 Concept 的獨立來源。

        C、D 皆由 B 衍生、B 由 A 衍生時，三者共用同一個原始 Evidence，只算 1 個。
        legacy_unknown 單獨計數，不進 independent_count、也不算使用者確認。
        """
        ids = tuple(dict.fromkeys(concept_ids))
        out = IndependentSupport(concept_ids=ids)
        units: dict[str, EvidenceUnit] = {}
        owners: dict[str, int] = {}
        missing_keys: set[tuple[str, str, tuple[str, ...]]] = set()
        cycle_keys: set[tuple[str, ...]] = set()

        for cid in ids:
            res = self.resolve(cid)
            if not res.found:
                key = ("concept", cid, ())
                if key not in missing_keys:
                    missing_keys.add(key)
                    out.missing.append(MissingRef("concept", cid, ()))
                out.per_concept[cid] = []
                continue
            out.per_concept[cid] = res.evidence_ids
            for unit in res.evidence:
                units.setdefault(unit.id, unit)
                owners[unit.id] = owners.get(unit.id, 0) + 1
            for m in res.missing:
                key = (m.kind, m.id, m.via)
                if key not in missing_keys:
                    missing_keys.add(key)
                    out.missing.append(m)
            for c in res.cycles:
                if c.cycle not in cycle_keys:
                    cycle_keys.add(c.cycle)
                    out.cycles.append(c)

        known_groups: set[str] = set()
        for uid, unit in units.items():
            if unit.tombstoned_at is not None:
                out.tombstoned_ids.append(uid)
            elif unit.provenance_status == "known":
                out.independent_ids.append(uid)
                known_groups.add(unit.episode_id or uid)
                if can_attest_user_claim(unit):
                    out.user_attested_ids.append(uid)
            else:
                out.legacy_unknown_ids.append(uid)
        out.independent_episode_count = len(known_groups)
        out.shared_ids = [uid for uid, n in owners.items() if n > 1]
        return out

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 內部
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    @staticmethod
    def _effective_derivations(concept: Any, version: int | None) -> list[Derivation]:
        """concept 在 version 當時已存在的衍生紀錄（含 legacy source_episode_ids）。"""
        out: list[Derivation] = []
        seen: set[tuple[str, str]] = set()
        for raw in concept.derivations:
            try:
                dv = Derivation.from_dict(raw)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning("[Provenance] 略過無效 derivation (%s): %s", concept.id, e)
                continue
            seen.add((dv.kind, dv.id))  # 即使被版本濾掉，也不可當作 legacy 來源回填
            if version is not None and dv.at_version > version:
                continue
            out.append(dv)
        for ep_id in concept.source_episode_ids:
            if ("episode", ep_id) not in seen:
                out.append(Derivation("episode", ep_id, "unknown", at_version=0))
                seen.add(("episode", ep_id))
        return out

    def _add_missing(self, ctx: _Ctx, kind: str, ref_id: str, via: Sequence[str]) -> None:
        key = (kind, ref_id, tuple(via))
        if key in ctx.missing_keys:
            return
        ctx.missing_keys.add(key)
        ctx.result.missing.append(MissingRef(kind, ref_id, tuple(via)))

    def _add_cycle(self, ctx: _Ctx, cycle: Sequence[str]) -> None:
        key = tuple(cycle)
        if key in ctx.cycle_keys:
            return
        ctx.cycle_keys.add(key)
        ctx.result.cycles.append(CycleReport(key))

    def _expand_concept(
        self,
        concept: Any,
        version: int | None,
        stack: list[str],
        steps: list[PathStep],
        ctx: _Ctx,
    ) -> None:
        ctx.expanded.add((concept.id, version))
        for dv in self._effective_derivations(concept, version):
            if dv.kind == "concept":
                self._follow_concept(dv, stack, steps, ctx)
            elif dv.kind == "episode":
                self._follow_episode(dv, stack, steps, ctx)
            else:
                self._follow_evidence(dv, stack, steps, ctx)

    def _follow_concept(
        self, dv: Derivation, stack: list[str], steps: list[PathStep], ctx: _Ctx,
    ) -> None:
        if dv.id in stack:
            self._add_cycle(ctx, stack[stack.index(dv.id):] + [dv.id])
            return
        parent = self._semantic.get(dv.id)
        if parent is None:
            self._add_missing(ctx, "concept", dv.id, stack)
            return
        if dv.parent_version is None:
            if dv.id not in ctx.result.unbound_versions:
                ctx.result.unbound_versions.append(dv.id)
        bound = dv.parent_version  # None＝未綁定，取目前狀態
        if (dv.id, bound) in ctx.expanded:
            return
        if len(stack) >= self._max_depth:
            ctx.result.truncated = True
            return
        shown = bound if bound is not None else parent.version
        self._expand_concept(
            parent, bound, stack + [dv.id],
            steps + [PathStep("concept", dv.id, shown, dv.producer)], ctx,
        )

    def _episode_units(self, ep_id: str, ctx: _Ctx) -> tuple[Any, list[EvidenceUnit]] | None:
        if ep_id not in ctx.episode_units:
            episode = self._get_episode(ep_id)
            if episode is None:
                ctx.episode_units[ep_id] = None
            else:
                ctx.episode_units[ep_id] = (episode, list(self._evidence.units_for_episode(episode)))
        return ctx.episode_units[ep_id]

    def _follow_episode(
        self, dv: Derivation, stack: list[str], steps: list[PathStep], ctx: _Ctx,
    ) -> None:
        found = self._episode_units(dv.id, ctx)
        if found is None:
            self._add_missing(ctx, "episode", dv.id, stack)
            return
        episode, units = found
        ep_steps = steps + [PathStep("episode", dv.id, None, dv.producer)]
        returned = {u.id for u in units}
        for ev_id in getattr(episode, "evidence_ids", None) or []:
            if ev_id not in returned:
                self._add_missing(ctx, "evidence", ev_id, stack)
        for unit in units:
            self._emit_evidence(unit, ep_steps, [], stack, ctx)

    def _follow_evidence(
        self, dv: Derivation, stack: list[str], steps: list[PathStep], ctx: _Ctx,
    ) -> None:
        got = self._evidence.get([dv.id])
        if not got:
            self._add_missing(ctx, "evidence", dv.id, stack)
            return
        self._emit_evidence(got[0], steps, [], stack, ctx, producer=dv.producer)

    def _emit_evidence(
        self,
        unit: EvidenceUnit,
        steps: list[PathStep],
        ev_stack: list[str],
        stack: list[str],
        ctx: _Ctx,
        producer: str | None = None,
    ) -> None:
        """記錄原始 Evidence；若 unit 本身另有 derived_from，改追到它的來源。"""
        if unit.id in ev_stack:
            self._add_cycle(ctx, ev_stack[ev_stack.index(unit.id):] + [unit.id])
            return
        step = PathStep("evidence", unit.id, None, producer)
        if unit.derived_from:
            parents = self._evidence.get(list(unit.derived_from))
            got = {p.id for p in parents}
            for pid in unit.derived_from:
                if pid not in got:
                    self._add_missing(ctx, "evidence", pid, stack)
            for parent in parents:
                self._emit_evidence(parent, steps + [step], ev_stack + [unit.id], stack, ctx)
            return
        if unit.id not in ctx.evidence:
            ctx.evidence[unit.id] = unit
            ctx.paths[unit.id] = ProvenancePath(tuple(steps + [step]))
