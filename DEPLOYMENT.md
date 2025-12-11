# U.E.P 部署指南

## 概述

本文檔說明如何將 U.E.P 專案打包成可部署的應用程式。

### 挑戰與解決方案

**主要挑戰：**
- 專案包含約 23 GB 的本地 AI 模型（bio_tagger + whisper）
- 複雜的依賴項（PyQt5、PyTorch、ONNX Runtime 等）
- TTS 模型已內建在模組中（IndexTTS Lite）

**解決方案：**
- 使用 PyInstaller 打包應用程式核心
- 大型模型文件（bio_tagger、whisper）獨立管理，不包含在打包結果中
- TTS checkpoints 隨應用程式一起打包
- 首次運行時檢查並指引用戶安裝缺失的模型

---

## 構建流程

### 前置要求

1. **Python 環境**
   - Python 3.10
   - 已安裝所有依賴項（`requirements.txt`）

2. **必要工具**
   ```bash
   pip install pyinstaller
   ```

3. **磁碟空間**
   - 開發環境：約 30 GB（包含模型）
   - 打包輸出：約 1-2 GB（含 TTS，不含 NLP/STT 模型）
   - 完整部署：約 25 GB（應用程式 + 全部模型）

### 快速開始

**Windows:**
```batch
# 激活虛擬環境
.\env\Scripts\activate

# 執行構建
.\build.bat
```

**或手動執行:**
```batch
# 激活虛擬環境
.\env\Scripts\activate

# 運行構建腳本
python build_app.py
```

### 構建輸出

構建完成後，會在 `dist/` 目錄下生成：

```
dist/
└── UEP_v{version}_{timestamp}/
    ├── UEP.exe                            # 主執行文件
    ├── start_uep.bat                      # Windows 啟動腳本
    ├── README_RELEASE.txt                 # 發行說明
    ├── _internal/                         # 依賴項和資源
    │   ├── modules/
    │   │   └── tts_module/
    │   │       └── checkpoints/           # ✓ TTS 模型已包含
    │   └── ...
    ├── configs/                           # 配置文件
    ├── models/
    │   ├── nlp/
    │   │   └── bio_tagger/                # ✗ 需要手動安裝
    │   ├── stt/
    │   │   └── whisper/                   # ✗ 需要手動安裝
    │   └── tts/                           # TTS 角色特徵檔 (.pt)
    ├── logs/                              # 日誌目錄
    └── memory/                            # 記憶目錄
```

---

## 模型管理

### 模型清單

專案需要以下模型（總計約 23 GB）：

| 類別 | 模型名稱 | 大小 | 狀態 | 用途 |
|------|---------|------|------|------|
| **NLP** | bio_tagger | ~0.1 GB | 需安裝 | 意圖識別和實體提取 |
| **STT** | whisper-large-v3 | ~23.2 GB | 需安裝 | 語音辨識 |
| **TTS** | IndexTTS Lite | ~0.5 GB | ✓ 已內建 | 語音合成（內建於模組） |
| **TTS** | 角色特徵 | < 0.1 GB | 需安裝 | 語音角色檔案 (uep.pt) |

### 模型安裝方法

#### 方法 1: 從開發環境複製（推薦）

```batch
# 複製 NLP 模型
xcopy /E /I /Y "原始專案\models\nlp\bio_tagger" "發行版\models\nlp\bio_tagger"

# 複製 STT 模型（注意：這個很大，約 23 GB）
xcopy /E /I /Y "原始專案\models\stt\whisper" "發行版\models\stt\whisper"

# 複製 TTS 角色特徵
xcopy /Y "原始專案\models\tts\*.pt" "發行版\models\tts\"
```

#### 方法 2: 手動下載

**Whisper Large V3:**
- 來源：https://huggingface.co/openai/whisper-large-v3
- 下載整個模型目錄到 `models/stt/whisper/whisper-large-v3/`

**Bio Tagger:**
- 這是訓練模型，需要從開發環境複製

---

## 部署策略

### 策略 1: 完整打包（約 25 GB）
適用於離線部署、內網環境

### 策略 2: 分離部署（推薦）
應用程式包（2 GB）+ 單獨的模型下載

### 策略 3: 最小化部署
僅應用程式包，使用文字輸入模式

---

## 故障排除

### 常見問題

1. **缺少 DLL** → 安裝 VC++ Redistributable
2. **找不到模型** → 按照模型安裝方法操作
3. **記憶體不足** → 需要 16GB+ RAM
4. **TTS 初始化失敗** → 檢查 checkpoints 目錄

詳細解決方案請參考完整文檔。

---

**文檔版本:** 1.0  
**最後更新:** 2025-12-11  
**維護者:** U.E.P 開發團隊
