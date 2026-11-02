# TestSeparateSTTSystem

> Standalone STT experiment extracted from U.E.P-s-Core — faster-whisper inference, multi-channel architecture, and production-grade post-processing pipeline.

## Project Overview

This repository is a focused extraction of the speech-to-text subsystem from [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core). The decoupling allows the STT engine to evolve independently — benchmarking model variants, validating VAD strategies, and hardening the multi-channel pipeline — without touching the stability of the main assistant.

The core module is `echo_stt`, built around faster-whisper with the `large-v3-turbo int8` model. The design prioritizes low VRAM footprint (~2 GB) and high throughput (1.2 s per 60 s of audio) while remaining composable: channels, workers, and the post-processing pipeline are independently configurable and testable.

The project shares a Python environment with U.E.P-s-Core (`env/`) and is designed to integrate back into the main system — specifically as the per-user voice channel STT backend for the U.E.P Discord Bot.

## Core Features

✯ **Speech Recognition Engine**
- 🔹 faster-whisper `large-v3-turbo int8` — 1.2 s / 60 s audio, ~2 GB VRAM
- 🔹 Multi-language support (Chinese / English / Japanese)
- 🔹 Runtime device switching (CPU ↔ CUDA) without model reload

✯ **Multi-Channel Architecture**
- 🔹 `MultiChannelSTTEngine` — unified entry point for all channel operations
- 🔹 `ChannelManager` — channel lifecycle management (create / pause / teardown)
- 🔹 `WorkerPool` — parallel transcription workers with queue overflow policy
- 🔹 `Aggregator` — cross-channel transcript merging with timebase alignment
- 🔹 `FileChannel` — audio file input for offline testing and benchmarking

✯ **Voice Activity Detection**
- 🔹 Dual VAD: Energy-based + Silero neural VAD in tandem
- 🔹 Adaptive trailing silence via `VadMuzzle`
- 🔹 Configurable speech duration thresholds per channel
- 🔹 Silero auto-rebuffer — accepts arbitrary chunk sizes without resampling errors

✯ **Post-Processing Pipeline**
- 🔹 Hallucination filter — repeated token / character detection + low-confidence rejection
- 🔹 Chinese Traditional conversion — OpenCC `s2twp` (Simplified → Traditional Taiwan vocabulary)
- 🔹 Configurable queue overflow policy (`drop_oldest`)

✯ **Speaker Identification**
- 🔹 pyannote.audio embedding extraction
- 🔹 Multi-distance scoring (cosine + Euclidean)
- 🔹 DBSCAN clustering for speaker grouping

✯ **Rich CLI**
- 🔹 Interactive shell with `config` / `mc` / `vad` / `device` sub-commands
- 🔹 Smoke test mode for fast environment validation
- 🔹 Real-time transcript display via Rich

## Project Structure

```
TestSeparateSTTSystem/
├── Entry.py               # CLI entry point (shell / smoke subcommands)
├── requirements.txt
├── run.bat
├── test.bat
├── configs/               # YAML configuration files
├── docs/                  # Design notes and API references
├── models/                # Downloaded model weights
├── outputs/               # Transcript output files
├── scripts/               # Utility scripts
├── tests/                 # pytest unit + integration tests
└── echo_stt/              # Core STT module
    ├── __init__.py
    ├── benchmarks/        # Performance benchmarks
    ├── channel/           # Multi-channel management (ChannelManager, FileChannel)
    ├── core/              # STT engine (faster-whisper wrapper), schemas, config
    ├── keyword/           # Smart keyword / wake-word detection
    ├── output/            # Transcript output formatting
    ├── pipeline/          # Post-processing (hallucination filter, OpenCC s2twp)
    ├── recognition/       # Recognition modes (manual / continuous / streaming)
    ├── speaker/           # Speaker identification (pyannote embedding)
    └── vad/               # Voice Activity Detection (Energy VAD + Silero VAD)
```

## Quick Start

### Prerequisites

- Python 3.10+ (via U.E.P-s-Core shared environment)
- CUDA-capable GPU recommended (~2 GB VRAM for `large-v3-turbo int8`)
- U.E.P-s-Core cloned at `../U.E.P-s-Core/` with its `env/` virtualenv populated

### Running

```powershell
# Point to U.E.P-s-Core's shared environment
$UEP_PYTHON = "..\U.E.P-s-Core\env\Scripts\python.exe"

# Interactive shell
& $UEP_PYTHON Entry.py shell

# Smoke test — validates model load and a short transcription round-trip
& $UEP_PYTHON Entry.py smoke

# Run the full test suite
& $UEP_PYTHON -m pytest tests/ -x -q
```

### Shell Commands

| Command | Description |
|---------|-------------|
| `config` | Show / update runtime configuration |
| `mc` | Multi-channel control (add / remove / list channels) |
| `vad` | Adjust VAD thresholds and silence windows |
| `device` | Switch inference device between CPU and CUDA |

## Technical Stack

| Category | Technology | Notes |
|----------|------------|-------|
| Language | Python 3.10+ | Type annotations + `match/case` |
| Inference | faster-whisper | CTranslate2-optimized Whisper backend |
| Model | large-v3-turbo int8 | ~1.5 GB on disk, ~2 GB VRAM, 1.2 s / 60 s audio |
| VAD | Silero VAD + Energy VAD | Dual-engine voice activity detection |
| Audio I/O | PyAudio | Real-time microphone capture |
| Speaker ID | pyannote.audio | Speaker embedding + DBSCAN clustering |
| Text conversion | OpenCC | `s2twp` — Simplified → Traditional Taiwan |
| CLI | Rich | Interactive terminal UI |
| Testing | pytest | Unit + integration tests |

## Development Status

### ✅ Phase A — Legacy Decoupling (Completed)
- 從 U.E.P Core 解耦 8 個檔案（約 3,400 行）
- 建立獨立 `echo_stt` 模組架構，取代原有的 `stt_module`

### ✅ Phase A2 — faster-whisper Engine (Completed)
- 從 transformers Whisper 遷移至 faster-whisper
- 模型升級至 `large-v3-turbo int8`：速度快約 15x，VRAM 消耗與 `base` 相近

### ✅ Phase B — Multi-Channel Engine (Completed)
- `MultiChannelSTTEngine` 統一入口
- `ChannelManager` + `WorkerPool` + `Aggregator` 完整串聯
- 25 個測試全部通過

### ✅ Phase B2 — Quality Optimizations (Completed)
- Hallucination 過濾（token 級 + 字元級重複偵測）
- OpenCC 繁簡轉換整合（`s2twp`）
- Silero VAD rebuffer 修復（任意 chunk size 自動對齊）
- Adaptive trailing silence (`VadMuzzle`)

### 📋 Next — Discord Integration
- 整合進 U.E.P Discord Bot
- Per-user voice channel → 多通道自然隔離
- STT → TTS 直連管線，完成語音完整迴路

## Related Projects

- [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) — Parent project and shared runtime environment
- [TestSeparateTTSSystem](https://github.com/Unforgettableeternalproject/TestSeparateTTSSystem) — TTS speech synthesis counterpart
- [TestSeperateMemorySystem](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem) — Echo Memory persistence system
- [TestSeparateSessionControl](https://github.com/Unforgettableeternalproject/TestSeparateSessionControl) — Session Control and conversation management

## License

MIT License
