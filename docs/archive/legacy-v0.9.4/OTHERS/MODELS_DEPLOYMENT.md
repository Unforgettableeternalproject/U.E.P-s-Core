# UEP 模型部署指南

## 概述

UEP 打包後的應用程式不包含大型 AI 模型（約 40GB），使用者需要手動放置模型文件才能使用完整功能。

## 為什麼模型不包含在打包文件中？

- **bio_tagger**: ~100MB (NLP 意圖識別)
- **whisper-large-v3**: ~23GB (語音轉文字)
- **TTS checkpoints**: 已內建在打包文件中

總計約 23GB+ 的模型文件，包含在安裝包中會導致：
- 下載/安裝時間過長
- 佔用過多磁碟空間
- 不需要某些功能的用戶也被強制下載

## 模型目錄結構

將 UEP.exe 解壓後，在 **UEP.exe 所在的目錄** 創建以下結構：

```
UEP_v0.8.8/
├── UEP.exe                    # 主程式
├── _internal/                 # PyInstaller 內部文件
│   └── ...
└── models/                    # 【手動創建】模型目錄
    ├── nlp/                   # NLP 模組模型
    │   └── bio_tagger/        # BIO 標註器模型
    │       ├── config.json
    │       ├── model.safetensors
    │       └── ...
    └── stt/                   # STT 模組模型
        └── whisper/
            └── whisper-large-v3/  # Whisper 大型模型
                ├── config.json
                ├── model.safetensors
                └── ...
```

## 快速設置步驟

### 1. 創建 models 目錄

在 `UEP.exe` 所在目錄下創建 `models` 資料夾：

```powershell
# Windows PowerShell
cd "C:\path\to\UEP_v0.8.8"
mkdir models
cd models
mkdir nlp, stt
cd nlp
mkdir bio_tagger
cd ..\stt
mkdir whisper
```

### 2. 下載或複製模型

#### 選項 A：從開發環境複製（推薦）

如果你有開發環境的完整模型：

```powershell
# 複製 NLP 模型
xcopy "C:\path\to\dev\models\nlp\bio_tagger" "C:\path\to\UEP_v0.8.8\models\nlp\bio_tagger" /E /I

# 複製 STT 模型
xcopy "C:\path\to\dev\models\stt\whisper\whisper-large-v3" "C:\path\to\UEP_v0.8.8\models\stt\whisper\whisper-large-v3" /E /I
```

#### 選項 B：使用模型下載工具（未來功能）

```powershell
# 將來會提供自動下載工具
.\UEP.exe --download-models
```

### 3. 驗證模型安裝

啟動 UEP.exe 並檢查日誌，應該看到：

```
[NLP] BIO標註器載入成功
[STT] Whisper 模型載入成功 (設備: cuda:0)
```

如果看到錯誤：

```
[ModelPathResolver] ✗ 未找到模型: nlp/bio_tagger
[ModelPathResolver]   請將模型放置於: C:\...\UEP_v0.8.8\models\nlp\bio_tagger
```

## 模型詳細說明

### NLP 模組 - bio_tagger

**用途**: 意圖識別和命令解析

**大小**: ~100MB

**必需文件**:
- `config.json` - 模型配置
- `model.safetensors` 或 `pytorch_model.bin` - 模型權重
- `tokenizer.json`, `vocab.txt` - 分詞器文件

**獲取方式**:
1. 從開發環境複製 `models/nlp/bio_tagger/`
2. 或從 Hugging Face 下載 `distilbert-base-uncased` 並訓練

**不安裝的影響**:
- NLP 模組將使用備用模式（基於規則的簡單匹配）
- 意圖識別準確度降低
- 複雜命令可能無法正確解析

### STT 模組 - whisper-large-v3

**用途**: 語音轉文字

**大小**: ~23GB

**必需文件**:
- `config.json` - 模型配置
- `model.safetensors` - 模型權重（多個分片）
- `preprocessor_config.json` - 預處理配置
- `tokenizer.json` - 分詞器

**獲取方式**:
1. 從開發環境複製 `models/stt/whisper/whisper-large-v3/`
2. 或從 Hugging Face 下載:
   ```python
   from transformers import WhisperForConditionalGeneration
   model = WhisperForConditionalGeneration.from_pretrained("openai/whisper-large-v3")
   model.save_pretrained("models/stt/whisper/whisper-large-v3")
   ```

**不安裝的影響**:
- STT 模組將嘗試從 Hugging Face 遠端載入（需要網路）
- 首次啟動會自動下載到 Hugging Face 快取目錄
- 啟動時間會非常長（下載 23GB）

### TTS 模組 - IndexTTS Lite

**用途**: 文字轉語音

**大小**: ~300MB（已內建）

**狀態**: ✅ 已包含在打包文件中

**位置**: `_internal/modules/tts_module/checkpoints/`

**不需要手動安裝**

## 進階配置

### 自定義模型路徑

如果你想將模型放在其他位置（例如外接硬碟），可以：

1. 創建符號連結（Windows 管理員權限）:

```powershell
# 將 models 目錄連結到外接硬碟
mklink /D "C:\path\to\UEP_v0.8.8\models" "E:\UEP_Models"
```

2. 修改配置文件（未來功能）:

```yaml
# configs/config.yaml
models:
  base_path: "E:/UEP_Models"
```

### 模型版本管理

不同版本的 UEP 可能需要不同版本的模型：

- v0.8.x: 當前模型版本
- v0.9.x: 可能升級 Whisper 到 v4

建議為每個 UEP 版本維護獨立的 models 目錄。

## 故障排除

### 問題：啟動時卡住很久

**原因**: STT 模組正在從網路下載 Whisper 模型

**解決**: 
1. 等待下載完成（一次性，約 20-30 分鐘）
2. 或手動放置模型文件到正確位置

### 問題：提示「模型不可用」

**檢查**:
1. models 目錄是否與 UEP.exe 在同一層級？
2. 目錄結構是否正確？
3. 模型文件是否完整？

**驗證**:
```powershell
# 應該看到這些文件
dir "models\nlp\bio_tagger\config.json"
dir "models\stt\whisper\whisper-large-v3\config.json"
```

### 問題：NLP 使用備用模式

**表現**: 日誌顯示 `[BIOTagger] 啟用備用模式`

**原因**: bio_tagger 模型未找到或載入失敗

**解決**:
1. 確認 `models/nlp/bio_tagger/` 包含所有必需文件
2. 檢查 config.json 是否有 `model_type` 字段
3. 確認模型文件未損壞

## 未來計劃

### 自動模型下載器（v0.9）

```powershell
# 互動式下載
.\UEP.exe --setup

# 選擇性下載
.\UEP.exe --download nlp  # 只下載 NLP 模型
.\UEP.exe --download stt  # 只下載 STT 模型
```

### 模型管理界面（v1.0）

- GUI 介面查看模型狀態
- 一鍵下載/更新模型
- 模型版本檢查
- 磁碟空間管理

## 聯繫支持

如果遇到模型相關問題：

1. 檢查日誌文件: `logs/full-YYYY-MM-DD_HH-MM-SS.log`
2. 搜尋 `[ModelPathResolver]` 相關信息
3. 提供錯誤信息到 GitHub Issues

---

**注意**: 本指南適用於 UEP v0.8.8 及以上版本。
