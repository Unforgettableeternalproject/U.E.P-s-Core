# NLP模組訓練數據需求分析

## 📊 完整模型所需數據量分析

### 基於BIO標註的序列標記模型數據需求

根據學術研究和實際經驗，對於多意圖分段的BIO標註任務：

#### 🎯 **最小可行數據量 (MVP)**
- **總樣本數**: 500-1000條標註語句
- **分佈比例**:
  - 單一意圖: 60% (300-600條)
  - 複合意圖: 40% (200-400條)
- **每類標籤**: 最少100個片段實例
- **平均句長**: 10-50個詞

#### 🚀 **推薦數據量 (Production Ready)**
- **總樣本數**: 2000-5000條標註語句
- **分佈比例**:
  - 單一意圖: 50% (1000-2500條)
    - CALL: 500-800條
    - CHAT: 600-1000條  
    - COMMAND: 800-1200條
  - 複合意圖: 50% (1000-2500條)
    - CALL+CHAT: 300-500條
    - CALL+COMMAND: 400-600條
    - CHAT+COMMAND: 200-400條
    - CALL+CHAT+COMMAND: 100-300條
- **每類標籤**: 200-500個片段實例
- **句長變化**: 5-100個詞的分佈

#### 🏆 **理想數據量 (Robust Model)**
- **總樣本數**: 5000-10000條標註語句
- **分佈均勻**: 每種意圖組合有足夠代表性
- **場景多樣化**: 涵蓋各種表達方式和語境

## 📁 數據儲存格式建議

### 1. **主要格式: CoNLL-U Plus** (推薦)
```
# sent_id = train_001
# text = Hello, are you there? I was thinking about the weather.
# intent_segments = [{"start": 0, "end": 21, "label": "CALL"}, {"start": 22, "end": 58, "label": "CHAT"}]
# metadata = {"speaker": "user", "scenario": "daily_chat", "complexity": "medium"}
1	Hello	_	_	_	_	_	_	B-CALL	_
2	,	_	_	_	_	_	_	I-CALL	_
3	are	_	_	_	_	_	_	I-CALL	_
4	you	_	_	_	_	_	_	I-CALL	_
5	there	_	_	_	_	_	_	I-CALL	_
6	?	_	_	_	_	_	_	I-CALL	_
7	I	_	_	_	_	_	_	B-CHAT	_
8	was	_	_	_	_	_	_	I-CHAT	_
9	thinking	_	_	_	_	_	_	I-CHAT	_
10	about	_	_	_	_	_	_	I-CHAT	_
11	the	_	_	_	_	_	_	I-CHAT	_
12	weather	_	_	_	_	_	_	I-CHAT	_
13	.	_	_	_	_	_	_	I-CHAT	_

```

**優點**:
- 標準化格式，工具支援好
- 包含豐富的元數據
- 便於版本控制
- 支援複雜的語言學標註

### 2. **輔助格式: JSON Lines** 
```json
{
  "id": "train_001",
  "text": "Hello, are you there? I was thinking about the weather.",
  "tokens": ["Hello", ",", "are", "you", "there", "?", "I", "was", "thinking", "about", "the", "weather", "."],
  "bio_labels": ["B-CALL", "I-CALL", "I-CALL", "I-CALL", "I-CALL", "I-CALL", "B-CHAT", "I-CHAT", "I-CHAT", "I-CHAT", "I-CHAT", "I-CHAT", "I-CHAT"],
  "segments": [
    {"text": "Hello, are you there?", "label": "CALL", "start": 0, "end": 21, "confidence": 1.0},
    {"text": "I was thinking about the weather.", "label": "CHAT", "start": 22, "end": 58, "confidence": 1.0}
  ],
  "metadata": {
    "speaker": "user",
    "scenario": "daily_chat",
    "complexity": "medium",
    "annotator": "expert_001",
    "annotation_date": "2025-08-21",
    "quality_score": 0.95
  }
}
```

**優點**:
- 程式處理方便
- 結構化數據
- 便於擴展欄位
- 適合機器學習流程

### 3. **備用格式: spaCy Binary**
```python
# 使用spaCy的DocBin格式儲存
import spacy
from spacy.tokens import DocBin

nlp = spacy.blank("en")
db = DocBin()

for example in training_data:
    doc = nlp(example["text"])
    # 添加span標註
    db.add(doc)

db.to_disk("./training_data.spacy")
```

**優點**:
- 高效儲存和載入
- spaCy生態系統整合好
- 支援複雜的標註層

## 🗂️ 建議的目錄結構

```
train/nlp/
├── data/
│   ├── raw/                    # 原始數據
│   │   ├── user_logs/         # 實際用戶對話記錄
│   │   ├── synthetic/         # 合成數據
│   │   └── external/          # 外部數據集
│   ├── annotated/             # 標註完成的數據
│   │   ├── train.conllu       # 訓練集 (70%)
│   │   ├── dev.conllu         # 開發集 (15%)
│   │   ├── test.conllu        # 測試集 (15%)
│   │   ├── train.jsonl        # JSON Lines格式
│   │   ├── dev.jsonl
│   │   └── test.jsonl
│   ├── metadata/              # 元數據
│   │   ├── annotation_guidelines.md
│   │   ├── label_definitions.json
│   │   └── quality_metrics.json
│   └── statistics/            # 數據統計
│       ├── label_distribution.json
│       ├── length_distribution.json
│       └── quality_report.json
├── scripts/
│   ├── data_preparation.py    # 數據預處理
│   ├── annotation_tool.py     # 標註工具
│   ├── quality_check.py       # 質量檢查
│   └── data_augmentation.py   # 數據增強
├── models/
│   ├── bio_tagger/           # BIO標註模型
│   ├── baselines/            # 基線模型
│   └── experiments/          # 實驗模型
└── results/
    ├── evaluation/           # 評估結果
    └── error_analysis/       # 錯誤分析
```

## 🔧 實際數據收集策略

### 階段一: 啟動期 (500-1000條)
1. **合成數據** (40%): 使用模式生成
2. **眾包標註** (30%): Amazon MTurk等平台
3. **專家標註** (20%): 高質量種子數據
4. **用戶日誌** (10%): 脫敏化的實際對話

### 階段二: 成長期 (1000-3000條)
1. **主動學習** (50%): 模型不確定性採樣
2. **用戶反饋** (30%): 線上學習收集
3. **數據增強** (20%): 基於現有數據的變體

### 階段三: 成熟期 (3000+條)
1. **持續收集** (60%): 用戶互動數據
2. **困難樣本** (25%): 針對性收集
3. **平衡調整** (15%): 少數類別補充

## ⚠️ 數據質量控制

### 標註一致性
- **Kappa係數**: >0.8 (高一致性)
- **多人標註**: 關鍵樣本3人以上標註
- **專家審核**: 隨機抽檢10%

### 數據驗證
- **邊界檢查**: segment位置準確性
- **標籤有效性**: 符合BIO規則
- **重複檢測**: 避免數據洩漏

### 增量更新
- **版本控制**: Git LFS管理大檔案
- **Schema驗證**: 確保格式一致性
- **自動測試**: CI/CD管道檢查

## 🎯 優先級建議

### 高優先級 (立即執行)
1. 建立標註規範和工具
2. 收集500條高質量種子數據
3. 實現基礎的BIO標註模型

### 中優先級 (短期目標)
1. 擴展到1500條標註數據
2. 實現主動學習流程
3. 建立質量評估體系

### 低優先級 (長期目標)
1. 大規模數據收集 (5000+)
2. 多語言支援
3. 跨域適應能力

## 💡 實用建議

1. **從小開始**: 先用500條數據建立基線
2. **質量優於數量**: 高質量的500條勝過低質量的2000條
3. **迭代改進**: 每次收集數據後重新訓練評估
4. **多元化來源**: 避免數據偏見
5. **自動化工具**: 減少手工標註成本

這個數據策略既確保了模型的實用性，又考慮了開發資源的限制。建議從MVP階段開始，逐步擴展到生產就緒的數據規模。
