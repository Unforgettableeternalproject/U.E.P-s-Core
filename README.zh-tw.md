# U.E.P 的核心 - v0.1.0 Stable

### 這份專案有提供多語言README供參考
[![Static Badge](https://img.shields.io/badge/lang-en-red)](./README.md) [![Static Badge](https://img.shields.io/badge/lang-zh--tw-yellow)](./README.zh-tw.md)

"Hello! My name is U.E.P, but you can call me U as well~"
"So the time finally came, and you got the chance to achieve your dream, that's pretty neat."

"Yeah, I am really excited about this project, who knows what I'll eventually become?"
"Probably become more annoying than usual, I hope that will not happen."

"Perhaps you'll be able to be like me as well?"
"Not in the next decade."

## 專案概述

U.E.P (Unforgettable Eternal Project) 是一個桌面AI助手專案，旨在創建一個擬人化的AI夥伴，具有語音交流、環境感知和任務自動化能力。本專案採用模組化架構設計，可靈活地啟用或禁用不同功能。

## 核心特性

✯ 系統架構:
- 🔹 高度模組化設計，核心與功能模組分離
- 🔹 彈性狀態管理系統，支援多種工作流程
- 🔹 先進的日誌系統，按類型和月份組織
- 🔹 強大的錯誤處理與回退機制
- 🔹 基於配置的動態功能啟用

✯ 主要功能:
- 🔹 多模組協同工作 (STT, NLP, MEM, LLM, TTS, SYS)
- 🔹 複雜工作流程引擎，支援多步驟操作
- 🔹 外部API整合 (如Gemini模型)
- 🔹 檔案處理、視窗控制和自動化任務
- 🔹 靈活的擴展機制，可動態載入模組

## 專案結構

```
U.E.P-s-Core/
├── arts/               # 藝術資源和視覺設計
├── configs/            # 全局及模組配置
├── core/               # 核心系統組件
│   ├── controller.py   # 主控制器
│   ├── module_base.py  # 模組基類
│   ├── registry.py     # 模組註冊表
│   ├── router.py       # 消息路由器
│   ├── session_manager.py # 會話管理
│   └── state_manager.py   # 狀態管理
├── devtools/           # 開發者工具
├── docs/               # 文檔和規範
├── logs/               # 日誌目錄 (按類型和月份組織)
├── memory/             # 記憶存儲
├── models/             # 模型文件
├── modules/            # 功能模組集合
│   ├── stt_module/     # 語音識別模組
│   ├── nlp_module/     # 自然語言處理
│   ├── mem_module/     # 記憶管理模組
│   ├── llm_module/     # 大型語言模型
│   ├── tts_module/     # 文本轉語音
│   └── sys_module/     # 系統功能模組
├── utils/              # 通用工具和輔助函數
└── Entry.py            # 程序入口點
```

## 安裝與配置

1. 克隆儲存庫
   ```
   git clone https://github.com/Unforgettableeternalproject/U.E.P-s-Core.git
   cd U.E.P-s-Core
   ```

2. 安裝依賴
   ```
   pip install -r requirements.txt
   ```

3. 配置設置
   - 編輯 `configs/config.yaml` 啟用所需模組
   - 各模組也有自己的配置文件在相應模組目錄中

4. 運行程序
   ```
   python Entry.py
   ```

## 當前開發狀態

⚑ 完成功能:
- ✅ 核心架構設計與實現
- ✅ 模組動態載入與註冊系統
- ✅ 日誌系統 (按類型和月份組織)
- ✅ 工作流程引擎框架
- ✅ 狀態管理系統
- ✅ 基礎文件處理工作流程

⚑ 進行中功能:
- ⏳ MEMModule 整合與快照結構優化
- ⏳ LLMModule 輸出格式標準化 (含情緒、指令)
- ⏳ 事件觸發系統完善
- ⏳ 前端模組 (UI/MOV/ANI) 整合

## 開發規劃

請參閱 `docs/第二階段進度.md` 查看詳細的開發路線圖，包括：
- 核心六大模組重構重點
- 前端三大模組規劃
- 前後端整合與開發協作重點

## 貢獻者

❦ 主要貢獻者:
- ඩ elise-love
- ඩ yutao33003

## 授權

本專案使用私有許可證，未經允許不得複製、修改或分發。
