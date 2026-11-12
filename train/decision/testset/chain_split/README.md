# 複合句拆分器評測集（PR-C0）

依 Chain 介面契約 3.10；標註判準見 `hidden_docs/架構重構/決策標註規格草案.md` §10。
全部為手寫的合成句，不含 Laya 訓練句，也不含真實對話。

| 檔案 | 用途 |
|---|---|
| `smoke.jsonl` | 第一階段 30 句 smoke 集（流程驗證用，統計意義不足） |
| `split_schema.py` | 列格式常數與驗證規則 |
| `validate.py` | 驗證器：`python validate.py smoke.jsonl --stats` |
| `eval_split.py` | 指標 1-6、9、10；7 需 `--laya`，8 需預測列帶 `first_segment_ms`／`total_ms` |
| `dedup_check.py` | 與 `out/`、`testset/` 的字串＋3-gram 近似去重 |

```text
python eval_split.py --gold smoke.jsonl --splitter identity     # 內建：identity／oracle／naive_punct／fault
python eval_split.py --gold smoke.jsonl --pred my_splitter.jsonl
python dedup_check.py smoke.jsonl --root ../.. --git-ref feature/decision-laya-data-r4
```

指標 3 為啟發式判定（段落含 resolves_to 且不再含代名詞），指標 10 為 proxy；
兩者的正式版需 LLM 判定與 Laya 重判。
