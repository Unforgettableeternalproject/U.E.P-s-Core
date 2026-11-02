# TTS Latency Profiling Plan

Metrics:

- `time_to_first_audio_ms`: user-perceived first playable audio latency.
- `gpt_ms`: autoregressive semantic-token generation time.
- `s2mel_ms`: semantic-to-mel generation time.
- `vocoder_ms`: waveform generation time.
- `rtf`: total synthesis time divided by generated audio duration.

Optimization sequence:

1. Establish repeatable measurements.
2. Preserve the existing IndexTTS2 quality path.
3. Split first-chunk latency from full-response throughput.
4. Pipeline GPT, S2Mel, and vocoder work where feasible.
