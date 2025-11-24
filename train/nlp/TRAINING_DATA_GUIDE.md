# 訓練資料添加指南

## 📋 資料格式說明

訓練資料使用 **JSONL** 格式（每行一個 JSON 對象），存放於：
```
train/nlp/nlp_training_data.jsonl
```

## ✅ 正確的 BIOS 標籤 (Stage 4)

只有 **5 個標籤** (COMPOUND 是系統層級判斷，不是 BIOS 標籤):
- **CALL**: 呼叫系統 ("Hello UEP", "Wake up assistant")
- **CHAT**: 聊天對話 ("天氣真好", "我今天很開心")
- **DIRECT_WORK**: 緊急工作指令 ("打開文件", "立刻設定鬧鐘")
- **BACKGROUND_WORK**: 可排隊的背景工作 ("同步設備", "背景下載更新")
- **UNKNOWN**: 無法識別的輸入

## 📝 資料結構範例

### 單一意圖範例
```json
{
  "id": "train_001",
  "text": "打開我的行事曆",
  "tokens": ["打開", "我的", "行事曆"],
  "bio_labels": ["B-DIRECT_WORK", "I-DIRECT_WORK", "I-DIRECT_WORK"],
  "segments": [
    {
      "text": "打開我的行事曆",
      "label": "DIRECT_WORK",
      "start": 0,
      "end": 7,
      "confidence": 1.0,
      "annotator_notes": ""
    }
  ],
  "metadata": {
    "source": "manual_annotation",
    "scenario": "work_command",
    "created_date": "2025-10-25",
    "annotated": true,
    "quality_checked": true,
    "annotator": "user_001"
  }
}
```

### 複合意圖範例 (多個分段)
```json
{
  "id": "train_002",
  "text": "Hello UEP，今天天氣真好，幫我打開文件",
  "tokens": ["Hello", "UEP", "，", "今天", "天氣", "真好", "，", "幫", "我", "打開", "文件"],
  "bio_labels": ["B-CALL", "I-CALL", "O", "B-CHAT", "I-CHAT", "I-CHAT", "O", "B-DIRECT_WORK", "I-DIRECT_WORK", "I-DIRECT_WORK", "I-DIRECT_WORK"],
  "segments": [
    {
      "text": "Hello UEP",
      "label": "CALL",
      "start": 0,
      "end": 9,
      "confidence": 1.0,
      "annotator_notes": ""
    },
    {
      "text": "今天天氣真好",
      "label": "CHAT",
      "start": 10,
      "end": 17,
      "confidence": 1.0,
      "annotator_notes": ""
    },
    {
      "text": "幫我打開文件",
      "label": "DIRECT_WORK",
      "start": 18,
      "end": 24,
      "confidence": 1.0,
      "annotator_notes": ""
    }
  ],
  "metadata": {
    "source": "manual_annotation",
    "scenario": "compound_interaction",
    "created_date": "2025-10-25",
    "annotated": true,
    "quality_checked": true,
    "annotator": "user_001"
  }
}
```

## 🔧 添加方式 1: 手動編輯 JSONL

直接在 `nlp_training_data.jsonl` 文件末尾添加新行（每個 JSON 對象一行）：

```bash
# 激活虛擬環境
.\env\Scripts\activate

# 用文本編輯器打開（或直接用 VS Code）
notepad train\nlp\nlp_training_data.jsonl
```

**注意事項**:
- 每個 JSON 對象必須在單獨一行
- 不要有多餘的空行或縮排
- `bio_labels` 長度必須等於 `tokens` 長度
- `start` 和 `end` 為字符位置（非 token 位置）

## 🔧 添加方式 2: 使用標註工具 (推薦大量數據)

```bash
# 激活虛擬環境
.\env\Scripts\activate

# 運行標註工具
python train\nlp\annotation_tool.py
```

標註工具特點：
- ✅ 自動生成正確的 BIO 標籤
- ✅ 自動計算 start/end 位置
- ✅ 提供 GUI 界面選擇意圖類型
- ✅ 自動驗證資料格式

## 🔧 添加方式 3: 批量生成工具

如果需要快速生成大量模板數據：

```bash
# 激活虛擬環境
.\env\Scripts\activate

# 運行數據生成器
python train\nlp\training_data_generator.py
```

生成器會根據預定義模板批量生成數據，然後你可以人工審核和調整。

## 📊 DIRECT_WORK vs BACKGROUND_WORK 區分原則

### DIRECT_WORK (緊急，可中斷聊天)
- 需要**立即執行**的指令
- 用戶**期待即時反饋**
- 會**中斷**當前聊天

**範例**:
- "打開文件" - 用戶需要立即查看
- "設定鬧鐘明天 7 點" - 時間敏感
- "刪除這個檔案" - 立即操作
- "顯示我的行程" - 需要即時查看

### BACKGROUND_WORK (可排隊等待)
- 可以在**背景執行**
- **不需要即時反饋**
- 不會中斷聊天，會排隊執行

**範例**:
- "同步我的設備" - 可在背景執行
- "下載這個更新" - 不需立即完成
- "備份我的資料" - 可慢慢執行
- "清理系統快取" - 不影響當前操作

## ⚠️ 常見錯誤

### ❌ 錯誤 1: BIO 標籤不一致
```json
{
  "tokens": ["打開", "文件"],
  "bio_labels": ["B-DIRECT_WORK"]  // ❌ 長度不符
}
```

✅ 正確:
```json
{
  "tokens": ["打開", "文件"],
  "bio_labels": ["B-DIRECT_WORK", "I-DIRECT_WORK"]  // ✅ 長度相同
}
```

### ❌ 錯誤 2: 使用 COMPOUND 標籤
```json
{
  "text": "UEP 打開文件",
  "bio_labels": ["B-COMPOUND", "I-COMPOUND", "I-COMPOUND"]  // ❌ COMPOUND 不是 BIOS 標籤
}
```

✅ 正確:
```json
{
  "text": "UEP 打開文件",
  "bio_labels": ["B-CALL", "B-DIRECT_WORK", "I-DIRECT_WORK"]  // ✅ 標記為兩個分段
}
```

### ❌ 錯誤 3: start/end 位置錯誤
```json
{
  "text": "打開文件",
  "segments": [
    {"text": "打開文件", "start": 0, "end": 2}  // ❌ 字符數不符
  ]
}
```

✅ 正確:
```json
{
  "text": "打開文件",
  "segments": [
    {"text": "打開文件", "start": 0, "end": 4}  // ✅ 4 個字符（中文）
  ]
}
```

## 📈 推薦數據量

- **MVP 測試**: 500-1000 條
- **生產就緒**: 2000-5000 條
- **高精度模型**: 5000-10000 條

**當前狀態**: ~800 條 (需擴充)

## 🎯 優先添加的數據類型

1. **DIRECT_WORK vs BACKGROUND_WORK 範例** (各 200 條)
2. **UNKNOWN 意圖範例** (100 條) - 模糊/歧義語句
3. **複合意圖範例** (300 條) - 包含多個意圖分段

## 🚀 快速開始

```bash
# 1. 激活環境
.\env\Scripts\activate

# 2. 查看當前資料量
python -c "with open('train/nlp/nlp_training_data.jsonl', 'r', encoding='utf-8') as f: print(f'當前資料量: {sum(1 for _ in f)} 條')"

# 3. 手動添加數據（編輯 JSONL 文件）
# 或運行標註工具
python train\nlp\annotation_tool.py

# 4. 訓練模型
python train\nlp\train_bio_model.py

# 5. 測試模型
python train\nlp\test_bio_model.py
```

## 📚 相關文件

- `data_requirements_analysis.md` - 詳細數據需求分析
- `annotation_tool.py` - 標註工具源碼
- `training_data_generator.py` - 數據生成器
- `train_bio_model.py` - 模型訓練腳本
