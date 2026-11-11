# U.E.P 的核心

### 這份專案有提供多語言README供參考
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## 專案概述

U.E.P (Unified Experience Partner) 是長壽命的個人 AI 助理核心。本 repo 現為**新 Core**（`uep/`）與遷入的子系統（`subsystems/`）。

> **Legacy 已移除**：舊的九模組事件驅動系統（`core/`、`modules/`、`utils/`、`devtools/`、`Entry.py`）已刪除，刪除前的最後狀態保留於 git tag `legacy-final-v0.9.4`；舊文件不再隨 repo 發布（需要時請查該 tag）。

## 專案結構

```
U.E.P-s-Core/
├── uep/                     # 新 Core（Presence／Bus／Orchestrator／Endpoint 等）
├── subsystems/              # 遷入的子系統（echo_memory、echo_stt、echo_tts、echo_thought_core）
├── tests/uep/               # Core 測試
├── scripts/                 # 閘門腳本、環境建置、Index 檢查
├── docs/
│   ├── 架構概觀.md          # 架構概觀（對外重點版）
│   └── index/               # Index／Tracking 查找表
├── train/nlp/               # NLP 訓練資料（Laya 資料集種子）
├── arts/                    # 概念圖
├── alembic.ini              # PostgreSQL migration
└── pyproject.toml / uv.lock # 環境（Python 3.12）
```

## 安裝與配置

- Python 3.12，由 `uv` 管理（`pyproject.toml` + `uv.lock`）；主要支援 Windows 10/11；多數 Core 測試需要 PostgreSQL。
- 建置／更新共用推理環境：`scripts/setup_infer_env.ps1`（預設 `~/.uep/venv-inference-312-t211`）。
- 全套測試閘門：`scripts/run_all_tests.ps1`；僅 Core 測試：`python -m pytest tests/uep -q`。
- 檢查 Index 表：`python scripts/check_index.py`。
- 驗收 Console：`python -m uep.console`（內部指南，不對外發布）。

## 文件資源

- **架構概觀**：`docs/架構概觀.md`（詳細設計與契約不公開）
- **東西在哪／做到哪**：`docs/index/`（`INDEX.md`、`TRACKING.md`）
- **Legacy（已棄用）**：git tag `legacy-final-v0.9.4`

## 貢獻者

❦ 主要貢獻者:
- ඩ unforgettableeternalproject (Bernie)
- ඩ elise-love
- ඩ yutao33003

## 授權

本專案使用私有許可證，未經允許不得複製、修改或分發。
