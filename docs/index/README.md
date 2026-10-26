# Index／Tracking 方案（T-02，決策 A2）

重構後專案的兩份查找表，目的是讓 agent 不用全 repo grep 就能定位。

| 文件 | 回答的問題 |
|---|---|
| [INDEX.md](INDEX.md) | 這個東西在哪？（模組、檔案、契約、事件） |
| [TRACKING.md](TRACKING.md) | 這個決策或驗收項做到哪？（D／A／V ↔ 任務卡 ↔ 實作 ↔ 測試） |
| [`scripts/check_index.py`](../../scripts/check_index.py) | 檢查兩份表沒有漏列、沒有指向不存在的檔案 |

## 方案取捨

| 面向 | 選擇 | 理由／代價 |
|---|---|---|
| 存放位置 | `docs/index/`（公開、進版控） | `hidden_docs/` 被 gitignore，agent 在乾淨 clone 讀不到。Tracking 內附 D／A 一行摘要表，不必依賴 hidden_docs 才看得懂編號 |
| 格式 | 純 Markdown 表格，每列第一欄是穩定 ID | 人與 agent 都能直接讀、可 grep（`grep "^| V07" docs/index/TRACKING.md`）。不採 YAML＋產生器：只有 36 個 V、21 個 D、約 11 張卡，手寫成本低於維護產生器 |
| 更新方式 | 手寫；誰改了 `uep/` 的模組、契約或事件，同一個 PR 更新對應列 | 腳本不產生內容，只檢查一致性。legacy 區段靜態連結 T-01 對照表，不掃 `core/`、`modules/` |
| 腳本範圍 | 只做檢查（標準庫、唯讀）；若 `uep/` 存在，另警告未列入 INDEX 的頂層子套件 | 產生器要處理「描述文字從哪來」，收益小；檢查器能抓到最常見的漂移（漏列、死連結） |
| CI | 先本機／PR 前手動執行；穩定後在 `ci.yml` 加一步 `python scripts/check_index.py`（見文末） | 現階段 uep/ 還在建骨架，過早進 CI 會因 planned 項目頻繁紅燈。腳本對 `planned` 列已寬容，可隨時接上 |
| 取捨 | 手寫會漂移；漂移靠檢查器的三條規則擋（V／D／A 編號齊全、路徑存在、T 卡編號合法） | 內容正確性（例如「狀態」欄是否過期）仍需人工；腳本不判斷 |

## 維護規則

1. **ID 欄不可改名**：V01–V36（提案第 23 章）、D1–D21、A1–A7、T-01–T-11 為穩定鍵，其他文件以它們互相引用。
2. **路徑用反引號**：`like/this.py` 或 `like/dir/`；腳本會檢查存在性。尚未存在的路徑，同一列必須含字樣 `planned`，到位後移除。
3. **證據分級**沿用 T-01：`讀碼`（推論）、`執行`（實際跑過）、`實測`（真實裝置／頻道）。「狀態」欄只在有對應證據文件時才往前推進，不得僅憑勾選。
4. **legacy 不逐檔建索引**：只連到 [Core現況對照表](../架構重構/Core現況對照表.md) 的章節。新 Core（`uep/`，決策 A7）才逐模組建索引。
5. 提案的 V 方框保持未勾（提案 A.4 要求保留原始基準），進度只記在 TRACKING.md。

## Agent 如何使用

查事件：
1. 新 Core 事件 → INDEX.md §4 查「事件名 → 定義檔 → 發布者／訂閱者」；定義與欄位以契約文件為準（INDEX §3 的連結）。
2. 舊事件（`SystemEvent` 44 項）→ INDEX.md §4 legacy 區段指到 T-01 對照表 §4.1 的分類表，那裡有 file:line。
3. 找不到才 `grep -rn "EVENT_NAME" uep/ core/ modules/`，並回頭補列。

查契約：INDEX.md §3 找契約名 → 取得「定義檔／文件章節」與「權威擁有者」→ 對應的 INV 與驗證案例在契約文件內；契約對應的 V 項在 TRACKING.md 以契約名 grep。

查驗收項（例如 V07）：
1. `grep "^| V07" docs/index/TRACKING.md` 得到：任務卡、實作路徑、測試路徑、證據文件、狀態。
2. 驗收條件原文在提案第 23 章（TRACKING 的「摘要」欄只是提示，不可取代原文）。
3. 實作欄為 `—` 表示尚無程式；證據欄為 `—` 表示尚無任何執行或讀碼證據。

查決策（例如 D5）：TRACKING.md §1 有一行摘要與狀態；詳述在 `hidden_docs/DECISIONS.md`（內部，clone 後可能不存在）與提案第 22 章 ADR。

查任務卡：TRACKING.md §3 為卡片總表；細節在任務層 change（`tasks(action="list")`）。

## 執行檢查

```bash
.venv/Scripts/python.exe scripts/check_index.py            # 檢查，有錯誤回傳 1
.venv/Scripts/python.exe scripts/check_index.py --json     # 機器可讀輸出
.venv/Scripts/python.exe -m unittest scripts.test_check_index
```

接 CI（待 uep/ 穩定後；本任務未改 `ci.yml`）：

```yaml
- name: Index／Tracking 一致性
  run: python scripts/check_index.py
```
