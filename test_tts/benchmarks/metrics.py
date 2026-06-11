from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean
from typing import Iterable


# ---------------------------------------------------------------------------
# Legacy LatencySample — kept for backward compatibility
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LatencySample:
    """One measured TTS synthesis sample (legacy placeholder).

    Durations are stored in milliseconds. `audio_duration_ms` is the generated
    waveform length, not wall-clock synthesis time.
    """

    text_chars: int
    audio_duration_ms: float
    gpt_ms: float
    s2mel_ms: float
    vocoder_ms: float
    first_audio_ms: float

    @property
    def total_ms(self) -> float:
        return self.gpt_ms + self.s2mel_ms + self.vocoder_ms

    @property
    def rtf(self) -> float:
        if self.audio_duration_ms <= 0:
            return 0.0
        return self.total_ms / self.audio_duration_ms


def summarize_latency(samples: Iterable[LatencySample]) -> dict[str, float | int]:
    collected = list(samples)
    if not collected:
        return {
            "sample_count": 0,
            "avg_total_ms": 0.0,
            "avg_first_audio_ms": 0.0,
            "avg_rtf": 0.0,
        }

    return {
        "sample_count": len(collected),
        "avg_total_ms": mean(sample.total_ms for sample in collected),
        "avg_first_audio_ms": mean(sample.first_audio_ms for sample in collected),
        "avg_rtf": mean(sample.rtf for sample in collected),
    }


# ---------------------------------------------------------------------------
# Stage-level profiling — new, richer data model
# ---------------------------------------------------------------------------

@dataclass
class InitProfile:
    """Timings captured during model initialization and character loading.

    All values are in milliseconds.  Zero means the stage was skipped or
    not measured.
    """

    gpt_ms: float = 0.0
    semantic_codec_ms: float = 0.0
    s2mel_ms: float = 0.0
    bigvgan_ms: float = 0.0
    emo_matrix_ms: float = 0.0
    text_processor_ms: float = 0.0
    char_load_ms: float = 0.0
    warmup_ms: float = 0.0

    @property
    def total_init_ms(self) -> float:
        return (
            self.gpt_ms
            + self.semantic_codec_ms
            + self.s2mel_ms
            + self.bigvgan_ms
            + self.emo_matrix_ms
            + self.text_processor_ms
        )

    @property
    def total_cold_start_ms(self) -> float:
        return self.total_init_ms + self.char_load_ms + self.warmup_ms

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> InitProfile:
        return cls(
            gpt_ms=d.get("gpt_ms", 0.0),
            semantic_codec_ms=d.get("semantic_codec_ms", 0.0),
            s2mel_ms=d.get("s2mel_ms", 0.0),
            bigvgan_ms=d.get("bigvgan_ms", 0.0),
            emo_matrix_ms=d.get("emo_matrix_ms", 0.0),
            text_processor_ms=d.get("text_processor_ms", 0.0),
            char_load_ms=d.get("char_load_ms", 0.0),
            warmup_ms=d.get("warmup_ms", 0.0),
        )


@dataclass
class SynthesisProfile:
    """Timings captured during a single synthesis call.

    All values are in milliseconds.  Stage timings (gpt_semantic_ms, etc.)
    are *accumulated* across all segments when text is split into multiple
    BPE-token segments.
    """

    text_chars: int = 0
    segment_count: int = 1
    text_norm_ms: float = 0.0
    emotion_ms: float = 0.0
    gpt_semantic_ms: float = 0.0
    gpt_latent_ms: float = 0.0
    s2mel_cfm_ms: float = 0.0
    vocoder_ms: float = 0.0
    wav_save_ms: float = 0.0
    total_wall_ms: float = 0.0
    audio_duration_ms: float = 0.0
    first_segment_ms: float = 0.0

    @property
    def synthesis_ms(self) -> float:
        """Pure synthesis time (text_norm through vocoder, excludes wav save)."""
        return (
            self.text_norm_ms
            + self.emotion_ms
            + self.gpt_semantic_ms
            + self.gpt_latent_ms
            + self.s2mel_cfm_ms
            + self.vocoder_ms
        )

    @property
    def rtf(self) -> float:
        """Real-time factor: wall time / audio duration.  < 1 = faster than real-time."""
        if self.audio_duration_ms <= 0:
            return 0.0
        return self.total_wall_ms / self.audio_duration_ms

    @property
    def first_audio_ms(self) -> float:
        """First-playable-audio latency.

        When multi-segment, this is the wall-clock time until the first
        segment's vocoder output is ready (measured from synthesis start).
        When single-segment, falls back to the sum of all stages.
        """
        if self.first_segment_ms > 0:
            return self.first_segment_ms
        return (
            self.text_norm_ms
            + self.emotion_ms
            + self.gpt_semantic_ms
            + self.gpt_latent_ms
            + self.s2mel_cfm_ms
            + self.vocoder_ms
        )

    @classmethod
    def from_dict(cls, d: dict[str, float]) -> SynthesisProfile:
        return cls(
            text_chars=int(d.get("text_chars", 0)),
            segment_count=int(d.get("segment_count", 1)),
            text_norm_ms=d.get("text_norm_ms", 0.0),
            emotion_ms=d.get("emotion_ms", 0.0),
            gpt_semantic_ms=d.get("gpt_semantic_ms", 0.0),
            gpt_latent_ms=d.get("gpt_latent_ms", 0.0),
            s2mel_cfm_ms=d.get("s2mel_cfm_ms", 0.0),
            vocoder_ms=d.get("vocoder_ms", 0.0),
            wav_save_ms=d.get("wav_save_ms", 0.0),
            total_wall_ms=d.get("total_wall_ms", 0.0),
            audio_duration_ms=d.get("audio_duration_ms", 0.0),
            first_segment_ms=d.get("first_segment_ms", 0.0),
        )


@dataclass
class ProfileReport:
    """Combined cold-start + synthesis profiling report."""

    init: InitProfile = field(default_factory=InitProfile)
    synthesis: SynthesisProfile = field(default_factory=SynthesisProfile)
    success: bool = True

    def format_report(self) -> str:
        """Human-readable profiling report for terminal output."""
        lines: list[str] = []
        s = self.synthesis
        i = self.init

        lines.append("═══════════════════════════════════════════════")
        lines.append("  TTS Profiling Report")
        lines.append("═══════════════════════════════════════════════")

        # Cold start section (only if we have init data)
        if i.total_init_ms > 0:
            lines.append("")
            lines.append("  ◆ Cold Start (init + character + warmup)")
            lines.append(f"    GPT model load        {i.gpt_ms:>10.1f} ms")
            lines.append(f"    Semantic Codec load    {i.semantic_codec_ms:>10.1f} ms")
            lines.append(f"    S2Mel model load       {i.s2mel_ms:>10.1f} ms")
            lines.append(f"    BigVGAN vocoder load   {i.bigvgan_ms:>10.1f} ms")
            lines.append(f"    Emo/Spk matrices       {i.emo_matrix_ms:>10.1f} ms")
            lines.append(f"    Text processor         {i.text_processor_ms:>10.1f} ms")
            lines.append(f"    Character load         {i.char_load_ms:>10.1f} ms")
            lines.append(f"    Warmup                 {i.warmup_ms:>10.1f} ms")
            lines.append(f"    ─────────────────────────────────────")
            lines.append(f"    Total cold start       {i.total_cold_start_ms:>10.1f} ms")

        # Synthesis section
        seg_label = f", {s.segment_count} segments" if s.segment_count > 1 else ""
        lines.append("")
        lines.append(f"  ◆ Synthesis ({s.text_chars} chars{seg_label})")
        lines.append(f"    Text normalization     {s.text_norm_ms:>10.1f} ms")
        lines.append(f"    Emotion processing     {s.emotion_ms:>10.1f} ms")
        lines.append(f"    GPT semantic gen       {s.gpt_semantic_ms:>10.1f} ms")
        lines.append(f"    GPT latent forward     {s.gpt_latent_ms:>10.1f} ms")
        lines.append(f"    S2Mel / CFM            {s.s2mel_cfm_ms:>10.1f} ms")
        lines.append(f"    BigVGAN vocoder        {s.vocoder_ms:>10.1f} ms")
        lines.append(f"    WAV save               {s.wav_save_ms:>10.1f} ms")
        lines.append(f"    ─────────────────────────────────────")
        lines.append(f"    Total wall time        {s.total_wall_ms:>10.1f} ms")

        # Summary
        lines.append("")
        lines.append("  ◆ Summary")
        lines.append(f"    Audio duration         {s.audio_duration_ms:>10.1f} ms")
        lines.append(f"    RTF                    {s.rtf:>10.3f}")
        rtf_label = "faster" if s.rtf < 1.0 else "slower"
        if s.rtf > 0:
            lines.append(f"    → {1/s.rtf:.1f}x {rtf_label} than real-time")
        lines.append(f"    First audio proxy      {s.first_audio_ms:>10.1f} ms")

        lines.append("═══════════════════════════════════════════════")
        return "\n".join(lines)


def summarize_profiles(profiles: Iterable[SynthesisProfile]) -> dict[str, float | int]:
    """Summarize multiple synthesis profiles into averaged statistics."""
    collected = list(profiles)
    if not collected:
        return {
            "sample_count": 0,
            "avg_total_wall_ms": 0.0,
            "avg_synthesis_ms": 0.0,
            "avg_first_audio_ms": 0.0,
            "avg_audio_duration_ms": 0.0,
            "avg_rtf": 0.0,
            "avg_gpt_semantic_ms": 0.0,
            "avg_gpt_latent_ms": 0.0,
            "avg_s2mel_cfm_ms": 0.0,
            "avg_vocoder_ms": 0.0,
        }

    return {
        "sample_count": len(collected),
        "avg_total_wall_ms": mean(p.total_wall_ms for p in collected),
        "avg_synthesis_ms": mean(p.synthesis_ms for p in collected),
        "avg_first_audio_ms": mean(p.first_audio_ms for p in collected),
        "avg_audio_duration_ms": mean(p.audio_duration_ms for p in collected),
        "avg_rtf": mean(p.rtf for p in collected),
        "avg_gpt_semantic_ms": mean(p.gpt_semantic_ms for p in collected),
        "avg_gpt_latent_ms": mean(p.gpt_latent_ms for p in collected),
        "avg_s2mel_cfm_ms": mean(p.s2mel_cfm_ms for p in collected),
        "avg_vocoder_ms": mean(p.vocoder_ms for p in collected),
    }
