# 複合句拆分器評測集（PR-C0）

依 Chain 介面契約 3.10；標註判準見 `hidden_docs/架構重構/決策標註規格草案.md` §10。
全部為手寫或模板組出的合成句，不含 Laya 訓練句，也不含真實對話。

| 檔案 | 用途 |
|---|---|
| `smoke.jsonl` | 階段 1 的 30 句 smoke 集（流程驗證用，統計意義不足） |
| `formal.jsonl` | **定稿版，評測請用這份**：階段 2 的 302 列（11/13 抽查後重標、accept 收緊、修模板痕跡）加難題集 cs-0303 至 cs-0414（112 列），共 414 列 |
| `formal_asr.jsonl` | 同一份標準答案、文字改成模擬語音辨識輸出（標點大幅減少且不規則、英文全小寫、無書名號），用來測拆分器對無標點輸入的切點與稱呼處理；由 `make_asr.py` 以固定 seed（預設 20261113）確定性產生，段落 `source` 依字元位移對照表重算，通過 `validate.py` |
| `make_asr.py` | `python make_asr.py [--seed N]` 由 `formal.jsonl` 重新產生 `formal_asr.jsonl`（標點保留率 15%，其餘拿掉；`context` 不轉換） |
| `hard_unlabeled.jsonl`、`hard_a.jsonl`、`hard_b.jsonl` | 難題集的盲標檔、標註者 A（凍結版，仲裁前）與獨立標註者 B；一致率與仲裁見 `agreement_report.md` §8 |
| `formal_a.jsonl`、`formal_b.jsonl`、`formal_unlabeled.jsonl` | **階段 2 快照（舊規格）**：302 列、舊 accept 與舊受話者規則，**不再通過新版 `validate.py`**，只保留作 §1-§6 一致性報告的依據 |
| `agreement_report.md` | 一致性報告：§1-§6 階段 2、§7 抽查後修訂、§8 難題集 A/B 與仲裁 |
| `split_schema.py` | 列格式常數與驗證規則（新增 `segments[].ambiguous`；accept 不得是 resolves_to 的子字串） |
| `validate.py` | 驗證器：`python validate.py formal.jsonl --stats` |
| `eval_split.py` | 指標 1-6、9、10；7 需 `--laya`，8 需預測列帶 `first_segment_ms`／`total_ms`；`ambiguous` 段落不計入指標 7、10 |
| `dedup_check.py` | 與 `out/`、`testset/`、smoke 的字串＋3-gram 近似去重 |

```text
python eval_split.py --gold formal.jsonl --splitter identity     # 內建：identity／oracle／naive_punct／fault
python eval_split.py --gold formal.jsonl --pred my_splitter.jsonl --review-out review.jsonl
python dedup_check.py formal.jsonl --root ../.. --git-ref feature/decision-laya-data-r4 --extra smoke.jsonl
```

## 新增內容摘要（11/13）

- **ambiguous 段落**：`segments[].ambiguous=true` 表示 addressed／route 無法確定（§10.5 第 5 條），`addressed`／`route` 只是名目值（true／`direct_chat`）。目前 formal 共 5 段（cs-0038／0167／0239 第 3 段與難題集 2 段）；`eval_split` 排除它們。
- **accept**：只放保留限定修飾語的等價寫法，不收光桿中心詞；作品名稱 `resolves_to` 不含書名號，判定時忽略書名號與引號。
- **難題集 cs-0303 起**（112 列，zh 58／en 54）：多候選先行詞 13、先行詞在 Assistant 回答 12、這個／那個／那裡 12、人稱代名詞 12、指向前段產出 14；只用「你」對他人下指令 10、「我們」提議 9、句尾稱呼 10、順帶提到她 10、ASR 誤聽稱呼 10。

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
- **需要人工抽查**：11/13 Bernie 已抽查 §10.4／§10.5 並據此改規格重標（見 `agreement_report.md` §7）。難題集的 A/B 一致率
  幾乎 100%，同樣只代表同類模型讀法一致；`notes` 標為邊界的列與 `ambiguous` 列仍建議人工複核。
- 尚未跑過任何 LLM 拆分器；上面的 baseline 結果只代表腳本可跑與指標會動。

## 雲端 LLM 基線（`llm_splitter.py`）

專案 Think 的 `UEP_LLM_*` 設定，模型 `gpt-6-luna`（Responses API、reasoning effort=low、並發 4、串流量測首段）。
prompt 只描述規則、只送 `text` 與 `context`，不含標註欄位或題目原句。逐筆快取於 `.llm_split_cache.jsonl`（`.gitignore` 排除），重跑不重送。
`--prompt v1` 是舊基線，`--prompt v2`（預設）是依新 §10.4／§10.5 改版：輸出用原文語言、無稱呼段落沿用最近的稱呼對象
（對別人說話就補回名字，對 Yuki 說不補）、只有她的資料或裝置才算對她說。

```text
python llm_splitter.py --prompt v2 --gold formal.jsonl --out pred_llm_v2_formal.jsonl --env-file <主 repo>/.env --summary-out llm_v2_formal_summary.json
python eval_split.py --gold formal.jsonl --pred pred_llm_v2_formal.jsonl --out eval_llm_v2_formal.json --review-out review_llm_v2_formal.jsonl
```

檔案：`pred_llm_{v2_smoke,v2_formal,v2_asr,v1_formal}.jsonl`、`eval_llm_*.json`（`_base`＝cs-0001..0302、`_hard`＝cs-0303..0414 的子集）、
`llm_*_summary.json`（token 與延遲）、`review_llm_v2_{smoke,formal,asr}.jsonl`（指標 3 複核抽樣）。
基於 HEAD 7eed3f6 的 `smoke.jsonl`、`formal.jsonl`（414 列）、`formal_asr.jsonl`（414 列）。

formal 結果（v1 用同一份新 gold 重跑；指標 3 為啟發式 proxy，主要分母）：

| 指標 | v1 全 414 | v2 全 414 | v1 base 302 | v2 base 302 | v1 hard 112 | v2 hard 112 | v2 asr 414 |
|---|---|---|---|---|---|---|---|
| 1 切點 F1 | 0.974 | 0.973 | 0.982 | 0.971 | 0.952 | 0.981 | 0.962 |
| 2 段數正確率 | 0.960 | 0.967 | 0.980 | 0.970 | 0.921 | 0.960 | 0.964 |
| 3 task_request | 0.909 | 0.910 | 0.952 | 0.975 | 0.870 | 0.857 | 0.912 |
| 3 agent_loop | 1.000 | 0.931 | 1.000 | 0.931 | - | - | 1.000 |
| 3 direct_chat | 0.923 | 1.000 | 1.000 | 1.000 | 0.800 | 1.000 | 0.923 |
| 4 依賴只回一段 | 0.991 | 1.000 | 0.990 | 1.000 | 1.000 | 1.000 | 0.991 |
| 5 漏拆率 | 0.030 | 0.026 | 0.010 | 0.020 | 0.069 | 0.040 | 0.020 |
| 6 稱呼保留 | 0.989 | 1.000 | 0.985 | 1.000 | 1.000 | 1.000 | 0.979 |
| 10 no_addressed proxy | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.0 | 0.010 |

延遲為 4 並發下的串流量測，v2 formal 首段 p50／p95 約 2.3／3.8 秒，遠高於首段 p95 ≤800ms 的暫定值。
