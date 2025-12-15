# U.E.P 的核心 - v0.9.4-stable (第三階段穩定發布)

### 這份專案有提供多語言README供參考
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## 專案概述

U.E.P (Unified Experience Partner) 是一個採用**事件驅動架構**的模組化桌面 AI 助理，具備語音互動、記憶管理、智慧工作流與桌面寵物功能。專案已完成**第二階段重構**並發布**第三階段穩定版本**（核心功能完成度 70-75%），實現了前後端橋接、特殊狀態機制、智能 VAD 啟動、使用者工具三視窗等核心功能，系統整體穩定可用。

## 核心特性

✯ **系統架構** (第二階段 - 事件驅動):
- 🔹 **Event Bus** - 20+ 種系統事件實現鬆耦合模組通訊
- 🔹 **三層處理模型** - 輸入層 → 處理層 → 輸出層，含 flow-based 去重機制
- 🔹 **三層會話管理** - General Session (GS) / Chatting Session (CS) / Workflow Session (WS)
- 🔹 **Working Context** - 協作通道 (CHAT_MEM, WORK_SYS) 實現跨模組資料交換
- 🔹 **狀態-會話一體化** - 狀態轉換自動創建對應會話
- 🔹 **Status Manager** - 動態追蹤情緒 (mood)、自尊 (pride)、樂於助人度 (helpfulness)

✯ **六大核心模組** (平均完成度 95%):
- 🔹 **STT** - Whisper-large-v3、VAD、說話人識別
- 🔹 **NLP** - BIOS 意圖分段、身份管理、狀態決策權
- 🔹 **MEM** - FAISS 向量資料庫、身份隔離記憶、快照系統
- 🔹 **LLM** - Gemini API 含上下文快取、MCP 客戶端、學習引擎
- 🔹 **TTS** - IndexTTS Lite、情感映射、分段串流
- 🔹 **SYS** - 工作流引擎含 9 大類別、MCP 伺服器、背景任務

✯ **前端模組** (第三階段進行中 - 平均完成度 81%):
- 🔹 **UI** - PyQt5 桌面疊加層、使用者工具三視窗、快速動作按鈕、主題系統
- 🔹 **ANI** - 事件驅動動畫控制器、情感表情映射
- 🔹 **MOV** - 狀態同步行為引擎、移動模式整合
- 🔹 **Frontend Bridge** - 統一前端模組管理與事件協調

✯ **第三階段新增特性**:
- 🌉 **前後端整合** - Frontend Bridge 實現完整的前後端協調與事件分發
- 🎯 **特殊狀態機制** - MISCHIEF（搗蛋）狀態含 5 種行為、LLM 驅動規劃
- 😴 **睡眠喚醒系統** - SLEEP 狀態資源釋放、快速喚醒機制
- 🗣️ **智能 VAD 啟動** - 基於 CALL 意圖的語音觸發，降低誤觸發
- 🎛️ **使用者工具** - 系統狀態/設定/監控三大面板、自動隱藏小工具
- ⚡ **快速動作** - 一鍵顯示/隱藏、ON_CALL 呼叫、睡眠切換
- 🎵 **完整媒體控制** - 背景播放監控、實時控制（播放/暫停/音量）
- 📊 **系統監控** - 模組健康度、效能指標、資源監控實時展示

## 專案結構

```
U.E.P-s-Core/
├── arts/                    # 藝術資源與動畫素材
├── configs/                 # 全局及模組配置
├── core/                    # 核心系統組件 (第二階段)
│   ├── controller.py        # 統一控制器含例外管理
│   ├── event_bus.py         # 事件驅動架構基礎
│   ├── framework.py         # 模組協調器與框架
│   ├── module_coordinator.py # 三層處理編排器
│   ├── registry.py          # 模組註冊表含能力
│   ├── router.py            # 舊版路由器 (第三階段清理)
│   ├── working_context.py   # 跨模組協作通道
│   ├── bases/               # 模組基礎類別
│   ├── sessions/            # GS/CS/WS 會話管理器
│   └── states/              # 狀態管理與佇列
├── devtools/                # 開發者工具與除錯 API
├── docs/                    # 文件 (SDD、階段進度)
│   └── SDD/                 # 系統設計文件
├── integration_tests/       # 端到端整合測試
├── logs/                    # 日誌目錄 (debug/runtime/error)
├── memory/                  # 持久化記憶與 FAISS 索引
├── models/                  # 機器學習模型 (Whisper, TTS, NLP)
├── modules/                 # 功能模組集合
│   ├── stt_module/          # 語音辨識含 VAD
│   ├── nlp_module/          # 自然語言處理含意圖分段
│   ├── mem_module/          # 記憶管理含身份隔離
│   ├── llm_module/          # 大型語言模型含上下文快取
│   ├── tts_module/          # 語音合成含情感控制
│   ├── sys_module/          # 系統工作流與 MCP 伺服器
│   ├── ui_module/           # 使用者介面 (第三階段)
│   ├── ani_module/          # 動畫控制器 (第三階段)
│   ├── mov_module/          # 移動行為 (第三階段)
│   └── frontend_integration.py # 前端協調器
├── utils/                   # 通用工具與輔助函數
├── wheel/                   # 預編譯套件 (不對外散布)
└── Entry.py                 # 程式進入點
```

## 安裝與配置

### 前置需求
- Python 3.10+
- CUDA 12.8+ (用於 GPU 加速)
- Windows 10/11 (主要支援平台)

### 安裝步驟

1. **複製存放庫**
   ```bash
   git clone https://github.com/Unforgettableeternalproject/U.E.P-s-Core.git
   cd U.E.P-s-Core
   ```

2. **建立虛擬環境**
   ```bash
   python -m venv env
   # Windows
   .\env\Scripts\activate
   # Linux/Mac
   source env/bin/activate
   ```

3. **安裝 PyTorch 含 CUDA** (手動步驟)
   ```bash
   # 適用於 RTX 40xx/50xx 系列顯示卡與 CUDA 12.8
   pip install torch==2.7.0+cu128 torchvision==0.22.0+cu128 torchaudio==2.7.0+cu128 \
     --index-url https://download.pytorch.org/whl/cu128
   ```
   > **注意**: PyTorch+CUDA 須單獨安裝，因特定 GPU 需求

4. **安裝其他依賴**
   ```bash
   pip install -r requirements.txt
   ```

5. **安裝預編譯套件** (來自 `wheel/` 目錄)
   ```bash
   # 部分套件需從 wheel/ 手動安裝
   # 這些套件因客製化編譯不對外散布
   pip install wheel/pyannote.audio-*.whl
   pip install wheel/fairseq-*.whl
   # ... (請查看 wheel/ 目錄中可用套件)
   ```

6. **配置設定**
   - 複製 `configs/config.yaml.example` 到 `configs/config.yaml` (如果存在)
   - 編輯 `configs/config.yaml` 以:
     - 設定你的 Gemini API 金鑰
     - 啟用/停用模組
     - 調整除錯等級
   - 各模組在 `modules/xxx_module/` 內有自己的 `config.yaml`

7. **執行程式**
   ```bash
   # 正式模式
   python Entry.py
   
   # 除錯模式 (互動式命令列)
   python Entry.py --debug
   
   # 除錯 GUI 模式
   python Entry.py --debug-gui
   ```

### 疑難排解
- **找不到 CUDA**: 確保 NVIDIA 驅動程式已更新
- **PyAudio 問題**: Linux 上可能需要 portaudio 函式庫
- **缺少 wheel 檔案**: 請聯繫維護者取得預編譯套件

## 開發狀態

### ✅ 第一階段 - 核心模組基礎 (已完成)
- 六大核心模組 (STT, NLP, MEM, LLM, TTS, SYS) 基本實作
- 模組註冊與動態載入
- 基礎工作流引擎
- 配置系統

### ✅ 第二階段 - 事件驅動架構 (已完成 - v0.7.4)
**架構轉型** (完成度 96%):
- ✅ Event Bus 含 20+ 種系統事件
- ✅ 三層處理模型 (輸入層/處理層/輸出層)
- ✅ 三層會話管理 (GS/CS/WS)
- ✅ Working Context 含協作通道
- ✅ 狀態-會話一體化
- ✅ Flow-based 去重機制

**模組重構** (平均完成度 95%):
- ✅ **STT**: VAD、Whisper-large-v3、說話人識別
- ✅ **NLP**: BIOS 分段、身份管理、狀態決策權
- ✅ **MEM**: FAISS 向量資料庫、身份隔離、快照系統 (100%)
- ✅ **LLM**: 上下文快取、MCP 客戶端、學習引擎
- ✅ **TTS**: IndexTTS Lite、情感映射、分段串流
- ✅ **SYS**: 工作流引擎、MCP 伺服器、9 類工作流 (100%)

**關鍵成就**:
- ✅ 會話-狀態統一生命週期
- ✅ MCP 協議整合實現 LLM tool-calling
- ✅ 身份隔離記憶含每使用者 FAISS 索引
- ✅ Status Manager 追蹤 mood/pride/helpfulness
- ✅ 關鍵路徑整合測試

### ✅ 第三階段 - 前後端整合 (穩定發布 - v0.9.4-stable)
**整體完成度**: 70-75% (核心功能穩定)

**已完成目標**:
- ✅ **目標 1 - 前後端整合** (90%)
  - Frontend Bridge 系統實現完整前後端協調
  - 使用者工具三視窗：系統狀態/使用者設定/系統監控 (95%)
  - 快速動作按鈕：顯示/呼叫/睡眠切換 (90%)
  - 自動隱藏小工具實現
  - 配置熱重載機制
  
- ✅ **目標 2 - 特殊狀態** (92%)
  - MISCHIEF 狀態 100% 完成：5 種行為、LLM 規劃引擎、執行統計
  - SLEEP 狀態 85% 完成：資源釋放、喚醒機制、動畫整合
  
- ✅ **目標 3 - VAD 進階** (90%)
  - 基於 CALL 意圖的智能啟動機制
  - VAD/文字輸入雙模式支援
  - 狀態感知的啟動控制
  
- ⚠️ **目標 4 - 工作流強化** (45%)
  - ✅ Play Media 工作流完整實現：背景監控、實時控制 (90%)
  - ✅ 記憶系統工具化：tool-based 檢索機制 (80%)
  - ❌ 子工作流系統尚未實現
  - ❌ 自然語言路徑解析待開發
  
- ✅ **目標 5 - 系統監控** (72%)
  - 模組效能指標自動收集 (85%)
  - User Gadget 與 Debug GUI 實時展示 (90%)
  - 基礎例外記錄機制 (40%)
  
- ⚠️ **目標 6 - 模組結構統一** (30%)
  - 部分模組已有子結構（MEM, SYS, UI, MOV）
  - 完整統一重構進行中

**核心成就**:
- ✅ Frontend Bridge 實現前後端事件驅動協調
- ✅ 雙重事件系統（系統 Event Bus + 前端 Event Bus）
- ✅ StatusManager 與前端模組完整整合
- ✅ MISCHIEF 行為系統 LLM 驅動規劃
- ✅ 配置熱重載與身份管理整合
- ✅ 效能指標歷史追蹤（最近 10 個快照）

**後續規劃**: v0.10.0 (Phase 3 完成) 預計 2026 Q1

### 📅 第四階段 - 平台適配 (未來)
- 多平台支援 (Windows/Linux/macOS)
- 效能優化
- 公開測試
- 正式部署

## 文件資源

- **系統設計**: `docs/SDD.md` - 完整系統架構文件
- **第二階段進度**: `docs/第二階段進度.md` - 第二階段規劃與目標
- **第三階段進度**: `docs/第三階段進度.md` - 第三階段詳細目標與路線圖
- **專案進度**: `docs/本學期的專案進度.md` - 整體專案狀況
- **API 參考**: `docs/SDD/` - 模組專屬設計文件

## 貢獻者

❦ 主要貢獻者:
- ඩ unforgettableeternalproject (Bernie)
- ඩ elise-love
- ඩ yutao33003

## 授權

本專案使用私有許可證，未經允許不得複製、修改或分發。
