# Project Structure

```text
TestSeparateTTSSystem/
├── Entry.py                         # CLI: smoke, benchmark, generate
├── README.md
├── QUICKSTART.md
├── PROJECT_STRUCTURE.md
├── DEVLOG.md
├── pyproject.toml
├── requirements.txt
├── benchmarks/                      # Future benchmark runners
├── docs/                            # Profiling and optimization notes
├── legacy_tts/                      # Copied U.E.P TTS source and local assets
│   ├── modules/tts_module/          # IndexTTS2 source snapshot
│   └── models/tts/uep.pt            # Local U.E.P voice asset, Git ignored
├── test_tts/                        # Standalone experiment API
│   ├── benchmarks/metrics.py
│   └── runtime/legacy_generate.py
└── tests/
```

First-phase goal: generate speech from this project without importing the main U.E.P Core package.
