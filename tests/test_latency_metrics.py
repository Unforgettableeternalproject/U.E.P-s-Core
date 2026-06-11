from test_tts.benchmarks.metrics import LatencySample, summarize_latency


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
