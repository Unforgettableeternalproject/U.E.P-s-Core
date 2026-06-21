# TestSeparateTTSSystem

> Standalone IndexTTS2 speech synthesis laboratory — migrated from U.E.P-s-Core for isolated latency profiling and inference optimization.

## Project Overview

TestSeparateTTSSystem is an experimental sandbox extracted from the main U.E.P-s-Core assistant to isolate the IndexTTS2 text-to-speech pipeline. By decoupling TTS work from the production assistant, each architectural change — emotion presets, streaming modes, stage profiling — can be measured and validated without risk to the live system.

The core module, `echo_tts`, wraps IndexTTS2 Lite (GPT semantic generation + S2Mel/CFM + BigVGAN vocoder) in a Rich interactive CLI. The persistent shell design separates cold-start model loading from warm synthesis, making latency experiments reproducible across sessions.

This project shares the U.E.P-s-Core Python environment (`env/`) rather than maintaining a separate virtualenv. The next integration target is the U.E.P Discord Bot, where `echo_tts` will serve as the voice synthesis backend.

## Core Features

✯ **Speech Synthesis Engine**
- 🔹 IndexTTS2 Lite — GPT semantic generation + S2Mel/CFM + BigVGAN vocoder pipeline
- 🔹 Emotion preset system — 12+ presets (calm, angry, cheerful, tsundere, and more) with per-segment variation
- 🔹 Speech speed control — `s2mel_stretch_ratio` adjustment at synthesis time
- 🔹 Mixed-language support — CJK + English segment splitting for natural pronunciation

✯ **Interactive CLI**
- 🔹 Persistent shell — cold start once, warm synthesis on every subsequent command
- 🔹 `say` / `save` / `profile` commands for flexible output modes
- 🔹 Emotion panel — real-time preset preview inside the shell
- 🔹 Batch mode — automated testing across all emotion presets

✯ **Performance Profiling**
- 🔹 Stage-level timing — GPT semantic, GPT latent, S2Mel/CFM, BigVGAN, WAV save measured independently
- 🔹 RTF (Real-Time Factor) calculation per synthesis run
- 🔹 First-playable-audio proxy latency tracking
- 🔹 Cold start vs. warm synthesis reported separately

✯ **Text Processing**
- 🔹 Chinese text normalization — numbers, dates, units converted to spoken form
- 🔹 Emphasis quote → comma pause conversion (「」『』)
- 🔹 English sentence segmentation for mixed-language input
- 🔹 Poise breathing — natural pause insertion between segments

## Project Structure

```
TestSeparateTTSSystem/
├── Entry.py                  # CLI entry point (shell / generate / profile / smoke)
├── run.bat                   # Launch interactive shell via U.E.P Core Python
├── test.bat                  # Run pytest via U.E.P Core Python
├── pyproject.toml
├── requirements.txt
├── echo_tts/
│   ├── __init__.py
│   ├── benchmarks/           # Stage profiling metrics (ProfileReport, RTF)
│   ├── cli/                  # Rich interactive CLI (shell / profile / help)
│   ├── config/               # Settings management + emotion_presets.yaml
│   ├── models/               # Emotion presets, speed control definitions
│   ├── modules/              # Core TTS backends (IndexTTS2Backend, Poise)
│   ├── runtime/              # Backend contract, session, legacy generate
│   └── utils/                # Text normalization, audio playback
├── tests/                    # pytest test suite
├── outputs/                  # Generated WAV files
└── docs/                     # Branching policy and design notes
```

## Quick Start

### Prerequisites

- Python 3.10+ (via `U.E.P-s-Core\env\`)
- PyTorch 2.7+ with CUDA 12.8
- GPU with CUDA support (IndexTTS2 requires GPU inference)
- U.E.P-s-Core repository cloned at the same parent directory level

### Running

```powershell
# Set the shared Python interpreter
$UEP_PYTHON = "..\U.E.P-s-Core\env\Scripts\python.exe"

# Interactive shell (recommended — cold start once, then warm synthesis)
& $UEP_PYTHON Entry.py shell

# Single synthesis pass
& $UEP_PYTHON Entry.py generate --text "你好，我是 Echo。" --output outputs/test.wav

# Stage profiling across N repetitions
& $UEP_PYTHON Entry.py profile --text "測試文字" --repeat 5

# Smoke test (validates scaffold without GPU inference)
& $UEP_PYTHON Entry.py smoke

# Full test suite
.\test.bat
```

### Shell Commands

| Command | Description |
|---------|-------------|
| `say <text>` | Synthesize, play immediately, discard temp file |
| `save --output <path> <text>` | Synthesize and write WAV to path |
| `profile <text>` | Synthesize with full stage-level timing report |
| `status` | Show loaded state, playback count, save count, last output path |
| `help` | List available commands |

## Technical Stack

| Category | Technology | Notes |
|----------|------------|-------|
| Python | 3.10+ | Type annotations + `match`/`case` |
| Inference | PyTorch 2.7 + CUDA 12.8 | GPU-accelerated synthesis |
| Acoustic model | IndexTTS2 | GPT + S2Mel/CFM + BigVGAN |
| Audio I/O | sounddevice + scipy | Playback and WAV file output |
| CLI | Rich | Interactive terminal UI with panels and tables |
| Testing | pytest | Unit and integration tests |
| Text processing | Custom normalizer | Chinese numbers, dates, unit conversion |

## Development Status

### ✅ Phase 1 — IndexTTS2 Migration (Completed)
- 從 U.E.P Core 遷出 IndexTTS2 程式碼
- 獨立生成語音驗證通過

### ✅ Phase 2 — Stage Profiling (Completed)
- 完整 stage-level timing instrumentation
- `ProfileReport` 資料模型 + CLI integration

### ✅ Phase 3 — Rich CLI + Streaming (Completed)
- Interactive shell with hot-reload backend
- Producer-consumer streaming architecture
- Fix-quick-streaming 即時播放

### ✅ Phase 4 — Emotion & Text Processing (Completed)
- 12+ emotion presets with per-segment variation
- Mixed-language segment splitting
- Chinese text normalization
- Emphasis quote pause conversion
- Poise breathing system

### 📋 Next — Discord Integration
- 整合進 U.E.P Discord Bot 做語音合成服務

## Related Projects

- [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) — Parent project and shared Python environment
- [TestSeperateMemorySystem](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem) — Echo Memory system
- [TestSeparateSTTSystem](https://github.com/Unforgettableeternalproject/TestSeparateSTTSystem) — Speech-to-text recognition
- [TestSeparateSessionControl](https://github.com/Unforgettableeternalproject/TestSeparateSessionControl) — Session control and conversation management

## License

MIT License
