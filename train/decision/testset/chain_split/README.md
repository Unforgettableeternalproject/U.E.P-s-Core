# 複合句拆分器評測集（PR-C0）

依 Chain 介面契約 3.10；標註判準見 `hidden_docs/架構重構/決策標註規格草案.md` §10。
全部為手寫或模板組出的合成句，不含 Laya 訓練句，也不含真實對話。

| 檔案 | 用途 |
|---|---|
| `smoke.jsonl` | 階段 1 的 30 句 smoke 集（流程驗證用，統計意義不足） |
| `formal_a.jsonl` | 階段 2 正式集，標註者 A 的完整標註（`cs-0001` 起） |
| `formal_unlabeled.jsonl` | 同一批句子，只留 id／lang／text／context，供標註者 B 盲標 |
| `split_schema.py` | 列格式常數與驗證規則 |
| `validate.py` | 驗證器：`python validate.py formal_a.jsonl --stats` |
| `eval_split.py` | 指標 1-6、9、10；7 需 `--laya`，8 需預測列帶 `first_segment_ms`／`total_ms` |
| `dedup_check.py` | 與 `out/`、`testset/`、smoke 的字串＋3-gram 近似去重 |

```text
python eval_split.py --gold formal_a.jsonl --splitter identity     # 內建：identity／oracle／naive_punct／fault
python eval_split.py --gold formal_a.jsonl --pred my_splitter.jsonl --review-out review.jsonl
python dedup_check.py formal_a.jsonl --root ../.. --git-ref feature/decision-laya-data-r4 --extra smoke.jsonl
```

## 注意：指標 3 與 10 是 proxy 算法

- **指標 3（代名詞補完）**目前用啟發式判定：補完後的段落必須含 `resolves_to`（或 `accept` 之一），
  且不能再含代名詞本身。這不是契約的 LLM 輔助判定。依契約，**至少 20% 抽樣要有人工或 LLM 複核**：
  `--review-out` 會寫出抽樣檔（預設 20%，`--review-rate` 可調），`human_or_llm` 欄位留給複核者填，
  判定函式 `judge` 也可替換成 LLM 判定。主要分母只算「拆成多段、會派發」的輸出（`value`），
  含退回單段的分母並列為 `all`。
- **指標 10** 用標註的 `addressed` 與稱呼保留與否推算，真值需要 Laya 對段落重判。
