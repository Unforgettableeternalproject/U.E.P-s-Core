# 複合句拆分器評測集（PR-C0）

依 Chain 介面契約 3.10；標註判準見 `hidden_docs/架構重構/決策標註規格草案.md` §10。
全部為手寫或模板組出的合成句，不含 Laya 訓練句，也不含真實對話。

| 檔案 | 用途 |
|---|---|
| `smoke.jsonl` | 階段 1 的 30 句 smoke 集（流程驗證用，統計意義不足） |
| `formal.jsonl` | **定稿版**：A 標註加 cs-0111 仲裁、§10.4 新粒度（評測請用這份） |
| `formal_a.jsonl` | 標註者 A 的標註（已同步 §10.4 粒度，與 formal.jsonl 只差 notes） |
| `formal_b.jsonl`、`agreement_report.md` | 標註者 B 的盲標與一致性報告、仲裁紀錄 |
| `formal_unlabeled.jsonl` | 同一批句子，只留 id／lang／text／context，供標註者 B 盲標 |
| `split_schema.py` | 列格式常數與驗證規則 |
| `validate.py` | 驗證器：`python validate.py formal.jsonl --stats` |
| `eval_split.py` | 指標 1-6、9、10；7 需 `--laya`，8 需預測列帶 `first_segment_ms`／`total_ms` |
| `dedup_check.py` | 與 `out/`、`testset/`、smoke 的字串＋3-gram 近似去重 |

```text
python eval_split.py --gold formal.jsonl --splitter identity     # 內建：identity／oracle／naive_punct／fault
python eval_split.py --gold formal.jsonl --pred my_splitter.jsonl --review-out review.jsonl
python dedup_check.py formal.jsonl --root ../.. --git-ref feature/decision-laya-data-r4 --extra smoke.jsonl
```

## 注意：指標 3 與 10 是 proxy 算法

- **指標 3（代名詞補完）**目前用啟發式判定：補完後的段落必須含 `resolves_to`（或 `accept` 之一），
  且不能再含代名詞本身。這不是契約的 LLM 輔助判定。依契約，**至少 20% 抽樣要有人工或 LLM 複核**：
  `--review-out` 會寫出抽樣檔（預設 20%，`--review-rate` 可調），`human_or_llm` 欄位留給複核者填，
  判定函式 `judge` 也可替換成 LLM 判定。主要分母只算「拆成多段、會派發」的輸出（`value`），
  含退回單段的分母並列為 `all`。
- **指標 10** 用標註的 `addressed` 與稱呼保留與否推算，真值需要 Laya 對段落重判。

## 限制

- **多樣性較低**：independent 句是從手寫的句子池（任務、閒聊、對他人說話、多步驟子句）加代名詞模板組出來的，
  同一子句會在多列重複，句型也比真實使用規整；hard negatives 是逐句手寫。
- **一致率可能偏高**：標註者 A 與 B 是同一類模型，A/B 一致（kind、切點、route 全 100%）不代表規格沒有歧義，
  也可能是共同誤讀。
- **需要人工抽查**：建議由 Bernie 或第三位標註者對 §10.4（`resolves_to` 粒度）與 §10.5（延續受話者、
  對他人說話後的無稱呼問句）相關的列抽查，特別是 `notes` 標為邊界的列。
- 尚未跑過任何 LLM 拆分器；上面的 baseline 結果只代表腳本可跑與指標會動。

## 雲端 LLM 基線（`llm_splitter.py`）

最簡 prompt（`PROMPT_VERSION=baseline-v1`：依賴只回一段、保留稱呼、補完指代）、專案 Think 的 `UEP_LLM_*` 設定，
模型 `gpt-6-luna`（Responses API、reasoning effort=low、並發 4、串流量測首段）。prompt 只含 `text` 與 `context`，不含任何標註欄位。
逐筆快取於 `.llm_split_cache.jsonl`（不入版控），重跑不重送。

```text
python llm_splitter.py --gold formal.jsonl --out pred_llm_formal.jsonl --env-file <主 repo>/.env --summary-out llm_formal_summary.json
python eval_split.py --gold formal.jsonl --pred pred_llm_formal.jsonl --out eval_llm_formal.json --review-out review_llm_formal.jsonl
```

跑的版本：`smoke.jsonl` blob `e9eaa0a2`、`formal.jsonl` blob `5a2ebedf`（commit d254809）；`formal_asr.jsonl` 當時尚不存在，未跑。

| 指標 | naive_punct | LLM 基線（formal 302 句） | 門檻 |
|---|---|---|---|
| 1 切點 F1 | 0.615 | 0.979 | 0.85 |
| 2 段數正確率 | - | 0.980 | 回報 |
| 3 代名詞 task_request／agent_loop／direct_chat | 0.163／0.000／0.000 | 0.952／1.000／1.000（n=42／29／8，主要分母） | 0.95／0.95／0.90 |
| 4 依賴只回一段召回 | 0.220 | 0.990 | 0.95 |
| 5 漏拆率 | - | 0.010 | 回報 |
| 6 稱呼保留 | 0.488 | 0.995 | 0.95 |
| 9 輸出驗證失敗 | - | 0.000 | 回報 |

指標 3 為啟發式 proxy，需看 `review_llm_formal.jsonl` 複核。延遲為 4 並發下的串流量測，首段 p50／p95 約 2.1／4.0 秒，
遠高於首段 p95 ≤800ms 的暫定值（effort=low 仍有推理 token，之後可比較 effort=none 與較小模型）。
