# TestSeparateSTTSystem

U.E.P (Unified Experience Partner) 的獨立 STT（語音轉文字）實驗專案。

從 [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) 遷出 `stt_module`，解耦後進行強化實驗。

## 模組架構

```
test_stt/
├── core/           # 核心辨識引擎（Whisper pipeline 包裝）
├── recognition/    # 辨識管線（手動/持續/串流模式）
├── vad/            # 語音活動偵測（VAD）
├── speaker/        # 語者識別（pyannote embedding + 多重距離）
├── keyword/        # 智能關鍵詞/喚醒詞偵測
└── benchmarks/     # 效能基準測試
```

## 從 U.E.P Core 遷出的元件

| 原始檔案 | 新位置 | 說明 |
|---------|--------|------|
| `stt_module.py` | `test_stt/core/` | Whisper 引擎 + 辨識管線 |
| `vad.py` | `test_stt/vad/` | 語音活動偵測 |
| `speaker_identification.py` | `test_stt/speaker/` | 語者識別系統 |
| `speaker_context_handler.py` | `test_stt/speaker/` | 語者上下文決策 |
| `smart_keyword_detector.py` | `test_stt/keyword/` | 智能喚醒詞偵測 |
| `schemas.py` | `test_stt/core/` | Pydantic 資料模型 |

## 環境需求

- Python 3.10+
- PyAudio
- transformers + torch
- pyannote.audio（語者識別，可選）
- 使用 U.E.P Core 的 venv

## 快速開始

```bash
# 使用 U.E.P Core 的 Python 環境
C:\Users\Bernie\source\repos\Unforgettableeternalproject\U.E.P-s-Core\env\Scripts\python.exe Entry.py
```

## 分支策略

- `master` — 穩定版本
- `develop` — 開發主線
- `feature/*` — 功能分支

## 關聯專案

- [U.E.P-s-Core](https://github.com/Unforgettableeternalproject/U.E.P-s-Core) — 母專案
- [TestSeparateTTSSystem](https://github.com/Unforgettableeternalproject/TestSeparateTTSSystem) — TTS 模組
- [TestSeperateMemorySystem](https://github.com/Unforgettableeternalproject/TestSeperateMemorySystem) — Echo Memory
- [TestSeparateSessionControl](https://github.com/Unforgettableeternalproject/TestSeparateSessionControl) — Session Control
