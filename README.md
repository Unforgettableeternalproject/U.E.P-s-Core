# Test Separate TTS System

Standalone experimental project for U.E.P local TTS migration, latency profiling, and inference optimization.

This repository is intentionally separate from `U.E.P-s-Core` so IndexTTS2 experiments can be measured and changed without destabilizing the main assistant.

## Phase 1 Goal

Move the existing U.E.P IndexTTS2 path into this project and verify it can generate speech directly from local copied assets.

## Phase 2 Goal

Instrument stage timings for GPT semantic generation, S2Mel, vocoder, first playable audio, and full-response RTF.

## Phase 3 Goal

Optimize first-audio latency while preserving emotion-vector control and speaker identity.
