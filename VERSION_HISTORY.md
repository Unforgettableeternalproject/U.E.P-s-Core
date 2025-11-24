# Version History

本文件記錄 U.E.P (Unified Experience Partner) 的版本歷史與變更說明。

## 版本標籤說明

- **v{major}.{minor}.{patch}**: 一般開發版本（develop/feature 分支）
- **v{version}-stable**: 穩定發布版本（合併到 main）
- **v{version}-patch-{num}**: 緊急修復版本（hotfix 分支）

---

## v0.7.4 (2025-11-24)
**類型**: Release Candidate  
**分支**: release/v0.7.4

### 主要更新
- ✅ **Phase 2 完成**: 事件驅動架構重構（完成度 96%）
- 🔄 **Event Bus 系統**: 實現 20+ 系統事件的中央調度
- 🏗️ **三層處理架構**: Input → Processing → Output with Flow-based deduplication
- 📦 **模組重構完成度**:
  - STT 模組: 90%
  - NLP 模組: 95%
  - MEM 模組: 100%
  - LLM 模組: 95%
  - TTS 模組: 90%
  - SYS 模組: 100%
- 🎨 **前端模組架構**: UI/MOV/ANI 基礎結構就緒（待 Phase 3 整合）
- 📝 **文檔更新**: README, 第三階段進度.md
- 📦 **依賴管理**: 完整的 requirements.in/txt（230+ packages）

### 技術債務
- Event Bus 與前端模組整合（Phase 3）
- MISCHIEF/SLEEP 狀態實現
- VAD 改進與整合測試

---

## v0.1.0-stable (2024)
**類型**: Stable Release  
**分支**: main

### 主要更新
- 🎉 **初始穩定版本發布**
- 🏗️ **基礎架構建立**:
  - 六大核心模組: STT, NLP, MEM, LLM, TTS, SYS
  - 基本的模組間通訊機制
  - 配置系統 (configs/config.yaml)
- 🗣️ **語音功能**:
  - Whisper STT 整合
  - Edge-TTS 語音合成
  - 基礎 VAD (Voice Activity Detection)
- 🧠 **NLP 與記憶**:
  - 意圖識別系統
  - FAISS 向量記憶
  - 對話上下文管理
- 🤖 **LLM 整合**:
  - Google Gemini API
  - 基礎提示詞系統
- 🎵 **多媒體功能**:
  - 音樂播放 (MOV 模組基礎)
  - 系統指令執行
- 📊 **開發工具**:
  - 調試系統 (debug_level 1-5)
  - 日誌系統 (log_level)
  - 基礎測試框架

### 已知問題
- 模組間耦合度高
- 缺乏統一的事件系統
- 狀態管理分散
- 前端整合不完整

---

## 版本間隙說明

從 v0.1.0-stable 到 v0.7.4 之間經歷了大量的迭代開發，但未正式標記版本。主要原因：
- Phase 1 (v0.2.x - v0.5.x): 功能快速迭代期，版本管理不規範
- Phase 2 (v0.6.x - v0.7.x): 架構重構期，多次重寫核心模組

從 v0.7.4 開始，採用嚴格的版本管理和自動標籤系統。

---

## 未來版本規劃

### v0.8.0 (Phase 3 - 計劃中)
- 前端模組完整整合
- Event Bus 全面連接
- MISCHIEF/SLEEP 狀態實現
- VAD 改進與效能優化
- 完整的整合測試覆蓋

### v1.0.0 (Phase 4 - 遠期目標)
- 生產環境部署就緒
- 完整的錯誤處理與恢復機制
- 效能優化與資源管理
- 完善的使用者文檔
- 持續整合/部署 (CI/CD)

---

*此文件由 GitHub Actions 自動維護*  
*最後更新: 2025-11-24*
