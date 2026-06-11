# Test Separate TTS System

Standalone experimental project for U.E.P local TTS migration, latency profiling, and inference optimization.

This repository is intentionally separate from `U.E.P-s-Core` so IndexTTS2 experiments can be measured and changed without destabilizing the main assistant.

## Phase 1 Goal

Move the existing U.E.P IndexTTS2 path into this project and verify it can generate speech directly from local copied assets.

## Phase 2 Goal

Instrument stage timings for GPT semantic generation, S2Mel, vocoder, first playable audio, and full-response RTF.

## Phase 3 Goal

Optimize first-audio latency while preserving emotion-vector control and speaker identity.

## Branching

Use develop for active work. Create future optimization tasks from develop as `feature/<short-topic>`. See docs/branching_policy.md.
## Persistent CLI

`Entry.py shell` keeps the copied IndexTTS2 backend alive across multiple commands. This separates cold-start model loading from warm-state synthesis experiments.

Inside the shell:

- `say <text>` generates temporary audio, plays it immediately, then deletes the temporary file.
- `save --output <path> <text>` generates an audio file without playback.
- `status` shows loaded state, playback count, save count, and the last output path.
