# Echo Memory System - 開發日誌

記錄每個階段的進度、決策與學習。

---

## 2026-04-06 - Phase 1 啟動

### 完成項目

#### 1. 專案基建
- ✅ 創建完整目錄結構
- ✅ 配置 `requirements.txt` 與 `pyproject.toml`
- ✅ 編寫詳細的 README.md
- ✅ 設置 `.gitignore` 與 `.env.example`

#### 2. 核心架構
- ✅ `MemoryEngine` 主類實作
- ✅ `Episode` Schema 定義
- ✅ `Concept` Schema 定義
- ✅ `MemoryContext` 容器實作

#### 3. 存儲層
- ✅ `EpisodicMemory` - FAISS + JSON 存儲
- ✅ `SemanticMemory` - 簡化版（佔位符）
- ✅ `WorkingMemory` - dict + TTL 短期緩存

#### 4. 檢索層
- ✅ `EcphoryRetrieval` - Phase 1 向量檢索

#### 5. 測試與示例
- ✅ 單元測試（`test_episodic.py`, `test_engine.py`）
- ✅ 最小對話示例（`minimal_chat.py`）
- ✅ 快速開始指南（`QUICKSTART.md`）

### 設計決策

#### 為何選擇 FAISS + NetworkX 而非 FalkorDB？

**理由**：
1. **無依賴**：避免 Docker 容器（OpenSoul 需要 FalkorDB Docker）
2. **輕量級**：純 Python 實作，便於調試
3. **可遷移**：Phase 2+ 可升級到 FalkorDB（保持 API 一致）

**trade-off**：
- 失去圖資料庫的 Cypher 查詢能力
- 需要手動實作圖遍歷（NetworkX）

#### Episode 存儲格式

選擇 **JSON 元數據 + NumPy 向量** 而非單一 Pickle 文件：

**優點**：
- JSON 可讀性高，便於調試
- NumPy 向量可直接用於 FAISS
- 模組化存儲，易於擴展

**缺點**：
- 兩個文件需要同步
- 略微複雜的序列化邏輯

#### MemoryContext 文本格式

實作了 OpenSoul 風格的相對時間與重要程度標記：

```
【相關對話記憶】
  - （剛才）[重要程度:《核心記憶》] 用戶提到喜歡晴天
```

**好處**：
- LLM 更容易理解記憶的時效性
- 顯著性分數轉換為自然語言（核心/重要/一般/淡薄）

### 學習筆記

#### Sentence Transformers 嵌入生成

```python
from sentence_transformers import SentenceTransformer

model = SentenceTransformer("all-MiniLM-L6-v2", device="cpu")
embedding = model.encode("text", convert_to_numpy=True)
# 輸出: numpy.ndarray, shape=(384,)
```

**注意**：
- `convert_to_numpy=True` 避免 Tensor 轉換開銷
- 384-dim 是 MiniLM-L6 的固定維度

#### FAISS 向量檢索

```python
import numpy as np

# 餘弦相似度檢索
query_vec = query_vec / np.linalg.norm(query_vec)  # 正規化
similarities = np.dot(embeddings, query_vec)
top_indices = np.argsort(similarities)[::-1][:k]
```

**為何不直接用 FAISS IndexFlatIP？**
- Phase 1 保持簡單，手動計算餘弦相似度
- Phase 2 可升級到 FAISS 的高效索引

### 待解決問題

#### 1. Embedding 生成性能

**現狀**：每次查詢都重新生成嵌入向量（~50ms）

**優化方向**：
- Working Memory 緩存查詢向量（TTL 5 分鐘）
- 批次生成嵌入（多個查詢一起處理）

#### 2. Episode 去重

**問題**：相同對話可能被重複存儲

**解決方案（Phase 2）**：
- 計算 Episode 嵌入向量的餘弦相似度
- 相似度 > 0.95 視為重複，合併而非新建

#### 3. 時間衰減未實作

**OpenSoul 公式**：
```
Recency = exp(-0.01 × hours_ago)
```

**計畫**：Phase 2 整合到檢索權重計算

---

## 下一階段計畫

### Phase 2: EcphoryRAG 多跳檢索（預計 2 週）

#### 目標
- [ ] 實作動態邊緣權重計算
- [ ] NetworkX 圖譜多跳展開
- [ ] 神經化學參數調控（seed_k, breadth_k）
- [ ] 性能優化（檢索 < 100ms）

#### 關鍵任務
1. **建立 Episode → Concept 連結**
   - 從對話中提取實體
   - 建立 MENTIONS 邊緣

2. **圖遍歷演算法**
   - BFS/DFS 多跳展開
   - 邊緣權重過濾（threshold）

3. **檢索策略整合**
   - 向量相似度找種子
   - 圖遍歷擴展相關節點
   - 結果排序與合併

---

## 備註

### 開發環境
- Python 3.10.11
- Windows 11 / PowerShell
- VS Code + Copilot

### 參考資料
- OpenSoul 架構文檔
- U.E.P SDD 系統設計文檔
- Sentence Transformers 官方文檔
