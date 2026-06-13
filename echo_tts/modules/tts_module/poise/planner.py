"""
PoisePlanner — 主協調器

整合 Scheduler、LevelSelector、FillerLibrary、EmotionCorrector、SfxLoader，
對外暴露唯一的介面：plan() → List[SegmentPlan]。

plan() 在段落迴圈開始前被呼叫，預先規劃整個序列，
標記哪些位置要插入緩衝段落以及對應的合成參數。
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Literal, Optional

from .config import PoiseConfig
from .emotion_corrector import EmotionCorrector
from .filler_library import FillerLibrary
from .level import PoiseLevel
from .level_selector import PoiseLevelSelector
from .scheduler import PoiseScheduler
from .sfx_loader import SfxLoader
from .state import PoiseState


@dataclass
class SegmentPlan:
    """
    A single step in the planned synthesis sequence.

    Each item is either a normal content segment or a poise rest segment.

    For normal segments:
        type='normal', original_seg_idx points to the original segments list.
    For poise segments:
        type='poise_silence', 'poise_audio', or 'poise_tts', with
        corresponding fields populated.
    """
    type: Literal['normal', 'poise_silence', 'poise_audio', 'poise_tts']

    # Normal segment fields
    original_seg_idx: Optional[int] = None

    # Poise segment fields
    poise_level: Optional[PoiseLevel] = None
    poise_silence_ms: Optional[float] = None      # For poise_silence
    poise_sfx_name: Optional[str] = None           # For poise_audio
    poise_text: Optional[str] = None               # For poise_tts
    poise_emotion: Optional[List[float]] = None    # For poise_tts

    # Post-rest arc_scale for the NEXT normal segment
    resume_arc_scale: float = 1.0

    def __repr__(self) -> str:
        if self.type == 'normal':
            return f"SegmentPlan(normal, seg={self.original_seg_idx})"
        label = self.poise_level.label if self.poise_level else "?"
        extra = ""
        if self.poise_text:
            preview = self.poise_text[:20] + "..." if len(self.poise_text) > 20 else self.poise_text
            extra = f", text=\"{preview}\""
        elif self.poise_silence_ms:
            extra = f", {self.poise_silence_ms:.0f}ms"
        return f"SegmentPlan({self.type}, level={label}{extra})"


# Silence duration ranges per level (ms)
_SILENCE_DURATION = {
    PoiseLevel.SILENCE: (200, 800),
}


class PoisePlanner:
    """
    Main coordinator for the Poise breathing system.

    Usage:
        planner = PoisePlanner(config)
        plan = planner.plan(
            segments=segments,
            total_segments=len(segments),
            language="zh",
            original_emotion=[0.1, 0.0, ...],
        )
        # plan is a List[SegmentPlan] to drive the synthesis loop
    """

    def __init__(
        self,
        config: PoiseConfig,
        seed: Optional[int] = None,
    ):
        self.config = config
        self._seed = seed
        self.scheduler = PoiseScheduler(config, seed=seed)
        self.level_selector = PoiseLevelSelector(config, seed=seed)
        self.filler_lib = FillerLibrary(seed=seed)
        self.emotion_corrector = EmotionCorrector(
            enabled=config.emotion_correction_enabled,
        )
        self.sfx_loader = SfxLoader(sfx_dir=config.sfx_path)
        self._state = PoiseState()

    def plan(
        self,
        total_segments: int,
        language: str = "zh",
        original_emotion: Optional[List[float]] = None,
        segments: Optional[List[list]] = None,
    ) -> List[SegmentPlan]:
        """
        Pre-plan the entire synthesis sequence.

        Produces a list of SegmentPlan items interleaving normal segments
        with poise rest segments where appropriate.

        Args:
            total_segments: Number of content segments from split_segments.
            language: Language code for filler selection ("zh" or "en").
            original_emotion: Base 8D emotion vector (before any arc offset).
            segments: Optional actual token lists — used for sentence-boundary
                      detection. If None, scheduler falls back to position-only.

        Returns:
            Ordered list of SegmentPlan items. Normal segments reference
            the original segment list by index.
        """
        # Reset all state for this run
        self._reset()

        # If disabled or too few segments, pass through as all-normal
        if not self.config.enabled or total_segments <= self.config.min_segments_to_trigger:
            return [
                SegmentPlan(type='normal', original_seg_idx=i)
                for i in range(total_segments)
            ]

        plan: List[SegmentPlan] = []
        pending_resume_scale = 1.0  # Will be set after a rest
        # Track which segments are deferred triggers (sentence-boundary shifted)
        _deferred_trigger_at: Optional[int] = None

        for seg_idx in range(total_segments):
            # Add the normal segment
            normal = SegmentPlan(
                type='normal',
                original_seg_idx=seg_idx,
                resume_arc_scale=pending_resume_scale,
            )
            plan.append(normal)
            pending_resume_scale = 1.0  # Reset after applying

            # Update accumulation
            self._state.accumulate()

            # Check for deferred trigger (shifted to sentence boundary)
            should_rest = False
            if _deferred_trigger_at is not None and seg_idx >= _deferred_trigger_at:
                should_rest = True
                _deferred_trigger_at = None

            # Check if scheduler wants to trigger here
            if not should_rest and self.scheduler.should_trigger(seg_idx, total_segments, self._state):
                if segments is not None:
                    # Find best position (prefer sentence boundary)
                    best = self.scheduler.find_best_trigger(
                        seg_idx, total_segments, self._state, segments,
                    )
                    if best == seg_idx:
                        should_rest = True
                    else:
                        # Defer to a later segment (sentence boundary)
                        _deferred_trigger_at = best
                else:
                    should_rest = True

            if should_rest:
                # Determine rest level
                level = self.level_selector.select(self._state)

                # Build the poise segment
                poise_seg = self._build_poise_segment(
                    level=level,
                    language=language,
                    original_emotion=original_emotion,
                )
                plan.append(poise_seg)

                # Get resume arc_scale for the next normal segment
                pending_resume_scale = self.emotion_corrector.get_resume_arc_scale(level)

                # Mark rest in state
                self._state.mark_rest(seg_idx)

        return plan

    def _build_poise_segment(
        self,
        level: PoiseLevel,
        language: str,
        original_emotion: Optional[List[float]],
    ) -> SegmentPlan:
        """Build a SegmentPlan for a poise rest segment."""
        import random

        if level == PoiseLevel.SILENCE:
            lo, hi = _SILENCE_DURATION[PoiseLevel.SILENCE]
            duration_ms = random.uniform(lo, hi)
            return SegmentPlan(
                type='poise_silence',
                poise_level=level,
                poise_silence_ms=duration_ms,
            )

        if level == PoiseLevel.BREATH:
            # Try to load a breath SFX; fallback to silence
            sfx = self.sfx_loader.get_breath()
            if sfx is not None:
                return SegmentPlan(
                    type='poise_audio',
                    poise_level=level,
                    poise_sfx_name='breath',
                )
            else:
                # Fallback: longer silence to represent breathing pause
                return SegmentPlan(
                    type='poise_silence',
                    poise_level=level,
                    poise_silence_ms=random.uniform(400, 1000),
                )

        # FILLER_LIGHT, FILLER_HEAVY, ACTION → TTS synthesis
        filler_text = self.filler_lib.get_filler(level, language)
        if filler_text is None:
            # Fallback to silence if no filler available
            return SegmentPlan(
                type='poise_silence',
                poise_level=level,
                poise_silence_ms=random.uniform(300, 600),
            )

        # Compute corrected emotion for the filler
        corrected_emotion = None
        if original_emotion is not None:
            corrected_emotion = self.emotion_corrector.compute_rest_emotion(
                original_emotion, level,
            )

        return SegmentPlan(
            type='poise_tts',
            poise_level=level,
            poise_text=filler_text,
            poise_emotion=corrected_emotion,
        )

    def _reset(self) -> None:
        """Reset all sub-components for a new synthesis run."""
        self._state.reset()
        self.scheduler.reset(seed=self._seed)
        self.level_selector.reset(seed=self._seed)
        self.filler_lib.reset(seed=self._seed)

    def get_state(self) -> PoiseState:
        """Get current poise state (for debugging)."""
        return self._state

    @staticmethod
    def format_plan_summary(plan: List[SegmentPlan]) -> str:
        """
        Format a human-readable summary of the plan.

        Example output:
            Poise Plan: 38 normal + 4 rest (2× 靜音, 1× 輕量銜接, 1× 敘事插入)
            Sequence: [N×8] → [靜音] → [N×6] → [輕量銜接] → [N×12] → [敘事插入] → [N×8] → [靜音] → [N×4]
        """
        normal_count = sum(1 for p in plan if p.type == 'normal')
        rest_count = sum(1 for p in plan if p.type != 'normal')

        # Count by level
        level_counts: dict = {}
        for p in plan:
            if p.poise_level is not None:
                label = p.poise_level.label
                level_counts[label] = level_counts.get(label, 0) + 1

        level_str = ", ".join(f"{v}× {k}" for k, v in level_counts.items())

        # Build sequence visualization
        parts: list = []
        run_count = 0
        for p in plan:
            if p.type == 'normal':
                run_count += 1
            else:
                if run_count > 0:
                    parts.append(f"[N×{run_count}]")
                    run_count = 0
                label = p.poise_level.label if p.poise_level else "?"
                parts.append(f"[{label}]")
        if run_count > 0:
            parts.append(f"[N×{run_count}]")

        header = f"Poise Plan: {normal_count} normal + {rest_count} rest"
        if level_str:
            header += f" ({level_str})"

        sequence = " → ".join(parts)
        return f"{header}\n  Sequence: {sequence}"
