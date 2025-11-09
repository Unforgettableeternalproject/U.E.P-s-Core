# U.E.P (Unified Experience Partner) 系統

## 系統設計文件
### System Design Documentation (SDD)

| 項目 | 內容 |
| :---: | :---- |
| 專案名稱 | U.E.P (Unified Experience Partner) |
| 專案代號 | UEP-Core |
| 撰寫日期 | 2025-11-07 |
| 計畫負責人 | Bernie |
| 系統版本 | v2.0 (Phase 2 - Event-Driven Architecture) |
| Python 版本 | Python 3.10+ |

---

## 專案概述 (Project Overview)

U.E.P 是一個**模組化 AI 助理系統**，採用**三層事件驅動架構 (Three-Layer Event-Driven Architecture)**，整合語音辨識 (STT)、自然語言理解 (NLP)、記憶管理 (MEM)、大型語言模型 (LLM)、系統工作流 (SYS)、語音合成 (TTS) 以及桌面寵物介面 (UI)，提供智慧型對話、工作流自動化、情感互動與長期記憶能力。

### 核心特性
- **事件驅動架構**: 透過 Event Bus 實現模組間鬆耦合通訊
- **三層處理模型**: Input → Processing → Output 層級清晰分離
- **會話管理**: 三層會話架構 (General/Chatting/Workflow Sessions)
- **智慧記憶**: FAISS 向量資料庫支援身份隔離的長期記憶
- **工作流自動化**: MCP 協議整合的 LLM 驅動工作流引擎
- **情感互動**: Status Manager 控制的動態情緒表達

---

## 文件結構 (Document Structure)

本系統設計文件分為以下章節：

### 核心文件
- **[第一章：系統架構](./SDD/01_系統架構.md)** - 整體架構、技術選型、介面設計
- **[第二章：核心子系統](./SDD/02_核心子系統.md)** - Controller, Framework, Event Bus, Sessions 等
- **[第三章：模組設計 - 輸入層](./SDD/03_模組設計_輸入層.md)** - STT, NLP 模組詳細設計
- **[第四章：模組設計 - 處理層](./SDD/04_模組設計_處理層.md)** - MEM, LLM, SYS 模組詳細設計
- **[第五章：模組設計 - 輸出層](./SDD/05_模組設計_輸出層.md)** - TTS, UI, MOV, ANI 模組詳細設計
- **[第六章：系統流程](./SDD/06_系統流程.md)** - CHAT/WORK 模式完整流程、事件驅動協作、Session 生命週期

---

## 快速導覽 (Quick Navigation)

### 想了解系統整體架構？
→ 閱讀 [第一章：系統架構](./SDD/01_系統架構.md)

### 想了解事件驅動機制？
→ 閱讀 [第二章：核心子系統 - Event Bus](./SDD/02_核心子系統.md#22-event-bus-事件匯流排)

### 想了解語音處理流程？
→ 閱讀 [第三章：STT 模組](./SDD/03_模組設計_輸入層.md#31-stt-模組) 和 [第五章：TTS 模組](./SDD/05_模組設計_輸出層.md#51-tts-模組)

### 想了解記憶系統？
→ 閱讀 [第四章：MEM 模組](./SDD/04_模組設計_處理層.md#41-mem-模組)

### 想了解工作流引擎？
→ 閱讀 [第四章：SYS 模組](./SDD/04_模組設計_處理層.md#43-sys-模組)

### 想了解完整對話流程？
→ 閱讀 [第六章：CHAT 模式流程](./SDD/06_系統流程.md#63-chat-模式完整流程-chat-mode-workflow)

### 想了解工作流執行流程？
→ 閱讀 [第六章：WORK 模式流程](./SDD/06_系統流程.md#64-work-模式完整流程-work-mode-workflow)

---

## 版本歷史 (Version History)

| 版本 | 日期 | 作者 | 說明 |
|:---:|:---:|:---:|:---|
| 2.0 | 2025-11-07 | Bernie | 重構為事件驅動架構，完成 Phase 2 |
| 1.0 | 2024 | Bernie | 初始版本 (直接調用架構) |

---

## 參考文獻 (References)

### 內部文件
- [完整系統流程文檔](./完整系統流程文檔.md)
- [事件驅動架構快速參考](./事件驅動架構快速參考.md)
- [三層架構實現深度分析](./三層架構實現深度分析.md)
- [架構深度分析與重構計劃](./架構深度分析與重構計劃.md)

### 外部技術文件
- [Whisper Documentation](https://github.com/openai/whisper)
- [Pyannote Audio](https://github.com/pyannote/pyannote-audio)
- [FAISS Documentation](https://github.com/facebookresearch/faiss)
- [Google Gemini API](https://ai.google.dev/docs)
- [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)

---

**最後更新**: 2025-11-07  
**維護者**: Bernie  
