# Legacy TTS Source Snapshot

This folder contains copied source code from `U.E.P-s-Core` for isolated migration work.

Excluded intentionally:

- `modules/tts_module/checkpoints/`
- runtime temp files
- Python caches
- voice/model weights under `models/tts/`

Keep large model artifacts outside Git and configure their paths through local settings when running heavy inference tests.
