from test_tts.benchmarks.metrics import (
    LatencySample,
    summarize_latency,
    InitProfile,
    SynthesisProfile,
    ProfileReport,
    summarize_profiles,
)


# ---------------------------------------------------------------------------
# Legacy LatencySample tests (backward compatibility)
# ---------------------------------------------------------------------------

def test_summarize_latency_computes_rtf_and_totals():
    sample = LatencySample(
        text_chars=40,
        audio_duration_ms=2000.0,
        gpt_ms=600.0,
        s2mel_ms=250.0,
        vocoder_ms=150.0,
        first_audio_ms=900.0,
    )

    summary = summarize_latency([sample])

    assert summary["sample_count"] == 1
    assert summary["avg_total_ms"] == 1000.0
    assert summary["avg_first_audio_ms"] == 900.0
    assert summary["avg_rtf"] == 0.5


# ---------------------------------------------------------------------------
# InitProfile tests
# ---------------------------------------------------------------------------

def test_init_profile_total_init_ms():
    ip = InitProfile(gpt_ms=100, semantic_codec_ms=50, s2mel_ms=80, bigvgan_ms=40, emo_matrix_ms=5, text_processor_ms=10)
    assert ip.total_init_ms == 285.0


def test_init_profile_total_cold_start_includes_char_and_warmup():
    ip = InitProfile(gpt_ms=100, char_load_ms=20, warmup_ms=500)
    assert ip.total_cold_start_ms == 100 + 20 + 500


def test_init_profile_from_dict():
    d = {"gpt_ms": 1200, "bigvgan_ms": 400, "unknown_key": 999}
    ip = InitProfile.from_dict(d)
    assert ip.gpt_ms == 1200
    assert ip.bigvgan_ms == 400
    assert ip.s2mel_ms == 0.0  # missing key defaults to 0


# ---------------------------------------------------------------------------
# SynthesisProfile tests
# ---------------------------------------------------------------------------

def test_synthesis_profile_rtf():
    sp = SynthesisProfile(
        text_chars=20,
        total_wall_ms=500.0,
        audio_duration_ms=2000.0,
    )
    assert sp.rtf == 0.25


def test_synthesis_profile_rtf_zero_duration():
    sp = SynthesisProfile(total_wall_ms=500.0, audio_duration_ms=0.0)
    assert sp.rtf == 0.0


def test_synthesis_profile_first_audio_ms():
    sp = SynthesisProfile(
        text_norm_ms=5,
        emotion_ms=2,
        gpt_semantic_ms=100,
        gpt_latent_ms=50,
        s2mel_cfm_ms=200,
        vocoder_ms=80,
    )
    assert sp.first_audio_ms == 5 + 2 + 100 + 50 + 200 + 80


def test_synthesis_profile_synthesis_ms_excludes_wav_save():
    sp = SynthesisProfile(
        text_norm_ms=5,
        emotion_ms=2,
        gpt_semantic_ms=100,
        gpt_latent_ms=50,
        s2mel_cfm_ms=200,
        vocoder_ms=80,
        wav_save_ms=15,
    )
    assert sp.synthesis_ms == 5 + 2 + 100 + 50 + 200 + 80
    assert sp.wav_save_ms == 15


def test_synthesis_profile_from_dict():
    d = {"gpt_semantic_ms": 120, "vocoder_ms": 80, "text_chars": 42}
    sp = SynthesisProfile.from_dict(d)
    assert sp.gpt_semantic_ms == 120
    assert sp.text_chars == 42


# ---------------------------------------------------------------------------
# ProfileReport tests
# ---------------------------------------------------------------------------

def test_profile_report_format_contains_key_sections():
    report = ProfileReport(
        init=InitProfile(gpt_ms=1000, bigvgan_ms=400),
        synthesis=SynthesisProfile(
            text_chars=30,
            gpt_semantic_ms=120,
            gpt_latent_ms=60,
            s2mel_cfm_ms=200,
            vocoder_ms=80,
            total_wall_ms=500,
            audio_duration_ms=2000,
        ),
    )
    text = report.format_report()
    assert "RTF" in text
    assert "Cold Start" in text
    assert "Synthesis" in text
    assert "GPT semantic" in text
    assert "BigVGAN" in text


def test_profile_report_format_hides_cold_start_when_zero():
    report = ProfileReport(
        synthesis=SynthesisProfile(
            text_chars=10,
            total_wall_ms=100,
            audio_duration_ms=500,
        ),
    )
    text = report.format_report()
    assert "Cold Start" not in text


# ---------------------------------------------------------------------------
# summarize_profiles tests
# ---------------------------------------------------------------------------

def test_summarize_profiles_empty():
    result = summarize_profiles([])
    assert result["sample_count"] == 0
    assert result["avg_rtf"] == 0.0


def test_summarize_profiles_averages():
    p1 = SynthesisProfile(
        total_wall_ms=400, audio_duration_ms=2000,
        gpt_semantic_ms=100, gpt_latent_ms=50, s2mel_cfm_ms=150, vocoder_ms=60,
    )
    p2 = SynthesisProfile(
        total_wall_ms=600, audio_duration_ms=2000,
        gpt_semantic_ms=200, gpt_latent_ms=50, s2mel_cfm_ms=250, vocoder_ms=60,
    )
    result = summarize_profiles([p1, p2])
    assert result["sample_count"] == 2
    assert result["avg_total_wall_ms"] == 500.0
    assert result["avg_gpt_semantic_ms"] == 150.0
