# EchoMemory MVP 品質基準規格書

## 向量設計原則

本資料集使用 **4 維人工向量**，各維度代表不同語意域，確保跨類別召回的可預測性。

| 類別 | 向量設計 | 說明 |
|------|----------|------|
| 食物 | `[0.9x, 0.1x, 0, 0]` | 第 1 維為主信號 |
| 電影 | `[0, 0, 0.9x, 0.1x]` | 第 3 維為主信號 |
| 程式設計 | `[0, 0, 0.1x, 0.9x]` | 第 4 維為主信號（與電影正交度低但方向不同） |
| 旅遊 | `[0.1x, 0.9x, 0, 0]` | 第 2 維為主信號 |

所有向量均已 L2 正規化（模長 = 1），確保餘弦相似度計算的正確性。

## Episodes 清單

| ID | Session | 類別 | 顯著性 | 說明 |
|----|---------|------|--------|------|
| ep_food_001 | session_food | 食物 | 0.8 | 牛肉麵偏好 |
| ep_food_002 | session_food | 食物 | 0.6 | 台北火鍋推薦 |
| ep_food_003 | session_food | 食物 | 0.9 | 蝦子過敏（重要記憶） |
| ep_movie_001 | session_movie | 電影 | 0.6 | 科幻電影推薦 |
| ep_movie_002 | session_movie | 電影 | 0.7 | 諾蘭電影偏好 |
| ep_prog_001 | session_prog | 程式設計 | 0.7 | Python GIL |
| ep_prog_002 | session_prog | 程式設計 | 0.6 | Python asyncio |
| ep_travel_001 | session_travel | 旅遊 | 0.6 | 京都景點 |
| ep_travel_002 | session_travel | 旅遊 | 0.7 | 歐洲簽證 |
| ep_graph_A | session_graph_test | 程式設計 | 0.5 | 記憶與學習（圖譜測試用，第 1 輪） |
| ep_graph_B | session_graph_test | 程式設計 | 0.6 | 機器學習記憶（圖譜測試用，第 2 輪） |
| ep_graph_C | session_graph_test | 程式設計 | 0.5 | 訓練資料多樣性（圖譜測試用，第 3 輪） |
| ep_diff_session | session_other | 程式設計 | 0.6 | ML 醫療應用（不同 session，圖譜隔離測試） |
| ep_high_salience | session_salience | 偏好 | 0.95 | 核心偏好設定（高顯著性） |
| ep_low_salience | session_salience | 閒聊 | 0.1 | 天氣閒聊（低顯著性） |

## Scenarios 列表

| ID | 名稱 | 驗收重點 |
|----|------|----------|
| SC-01 | 偏好記憶直接召回 | recall@3 >= 0.6，precision@3 >= 0.6 |
| SC-02 | 食物類別群集召回 | 混合庫中食物類排前，precision@3 >= 0.6 |
| SC-03 | 圖譜展開跨輪追問 | 同 session PRECEDES 邊展開，recall@3 >= 0.5 |
| SC-04 | 無關查詢空結果（預設閾值） | 返回空結果，不崩潰 |
| SC-05 | 無關查詢空結果（嚴格閾值） | min_similarity=0.35 過濾跨類別 |
| SC-06 | 空庫邊界 | 空庫不拋例外，返回空 MemoryContext |
| SC-07 | 單筆不匹配 | 單一不相關記錄不干擾查詢 |
| SC-08 | 時間戳基準 | 所有注入的 episode timestamp < T0 |
| SC-09 | 圖譜不跨 session | 不同 session 間無 PRECEDES 邊 |
| SC-10 | 高低顯著性基準 | 高顯著性 episode 排名優先 |

## MVP 門檻

以下為通過 MVP 驗收的最低要求：

| 指標 | 最低要求 |
|------|----------|
| 整體 recall@3 平均 | >= 0.5 |
| 整體 precision@3 平均 | >= 0.5 |
| 空結果精準率（empty_precision） | = 1.0（所有應空的 scenarios 必須為空） |
| 空庫不崩潰 | 必須通過（SC-06） |
| 跨類別隔離 | 必須通過（SC-04, SC-05） |

## 時間戳設計

- 基準時間 `T0 = datetime(2026, 1, 1, tzinfo=UTC)`
- 每個 episode 的 `timestamp = T0 - timedelta(seconds=t_offset_seconds)`
- `t_offset_seconds` 越大，表示記憶越舊

## 測試執行說明

```bash
# 執行品質基準測試（需安裝測試依賴）
pytest tests/test_benchmark_quality.py -v

# 執行延遲基準測試
python tests/benchmark/run_latency.py

# 執行品質計算腳本
python tests/benchmark/run_quality.py
```

## 注意事項

1. **不使用 sentence-transformers**：測試期間透過 monkeypatch 替換 `_generate_embedding`，直接使用 JSON 中的固定 4 維向量，避免模型下載依賴。
2. **測試隔離**：每個 scenario 使用獨立的 `tmp_path`，互不影響。
3. **向量正規化**：`EpisodicMemory.write()` 會自動 L2 正規化向量，JSON 中的向量是注入前的值。
