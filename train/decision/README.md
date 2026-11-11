# train/decision — Laya 決策模型訓練資料工具

> 設計：[`docs/架構重構/決策模型訓練設計草案.md`](../../docs/架構重構/決策模型訓練設計草案.md)（草案，待裁）；標註規格：[`決策標註規格草案.md`](../../docs/架構重構/決策標註規格草案.md)。
> 這裡是**純資料工具**，不 import `uep`，不改 production。**Laya 只用英文訓練**；題目與 state 值一律英文。

## 目錄

```
train/decision/
├── laya_data/
│   ├── kinds.py        # 每個決策 kind 一份 KindSpec（題目、state builder、expected 驗證）
│   ├── convert_nlp.py  # train/nlp -> laya-train JSONL（選用種子，只產 route）
│   ├── synth.py        # 情境卡合成器（卡片決定標籤，LLM 只生文字）
│   └── split.py        # train/val 切分、測試集防洩漏檢查
├── testset/zh_candidates_draft.jsonl   # 測試集 zh-TW 原句草稿（status=pending_review，待艾斯維爾核）
├── pool/           # synthetic 內容池（入庫；量產後才有）
├── pool_private/   # real_owner 日誌（.gitignore，不入 git）
└── out/            # 產物（.gitignore）
```

turn.reply 的題目與 state 取自 `uep/decision/laya_schema.py`（訓練與推論共用，不另寫）；助理名一律用 MT 佔位名 `Yuki`，測試集 zh 草稿中的「U.E.P」在 MT 後會成為 Yuki。

已實作 kind：`turn.reply`（reply_mode／route／addressed，沿用既有 adapter 題目 ID）、`presence.absent_intent`（`absent.intent`／`absent.target_location`）。新增 kind＝在 `kinds.py` 加一份 `KindSpec` 與 `synth.py` 的情境卡。

## laya-train 格式（laya 0.4.22 原始碼確認）

每列：`{"state", "questions", "expected"}`（可用 `gold` 取代 `expected`）。`expected` 只列有標註的題；沒列的題不計入。
同一題目 ID 在全資料集定義必須唯一（不同定義只會警告並保留第一個），所以跨 kind 同名題目要加前綴。
額外鍵（`kind`、`sample_id`、`source`、`meta`）會被忽略，用來放 metadata（只放 ID／列舉，不放原文）。

## 用法（於 `train/decision/` 下，使用共用 venv，只讀使用）

```bash
PY=$USERPROFILE/.uep/venv-inference-312-t211/Scripts/python.exe

# 1) train/nlp 種子（route）：印出對映表與丟棄統計
$PY -m laya_data.convert_nlp --out out/route_seed.jsonl

# 2) 合成：預設只列計畫（不連網）；要真的呼叫需 --confirm-api 與環境變數金鑰
$PY -m laya_data.synth --kind turn.reply --max-samples 200 --out out/synth_turn_reply.jsonl
LAYA_SYNTH_MODEL=gpt-5.6-luna $PY -m laya_data.synth --kind turn.reply --max-samples 200 --out out/synth_turn_reply.jsonl --confirm-api

# 3) 切分與測試集防洩漏
$PY -m laya_data.split split --data out/synth_turn_reply.jsonl --train-out out/train.jsonl --val-out out/val.jsonl
$PY -m laya_data.split check --train out/train.jsonl out/val.jsonl --test <測試集英文句 JSONL，每列含 text>
```

- 試產上限 200 筆；超過需 `--budget-confirmed`（量產等確認）。金鑰只從 `UEP_LLM_API_KEY`／`OPENAI_API_KEY` 環境變數讀取，不寫檔、不印出。
- 訓練（之後，另裝 laya 於獨立 venv，**不要裝進共用 venv**）：`laya-train --data out/train.jsonl --eval out/val.jsonl --base <model> --out <dir>`。

## 來源、授權與隱私

- `train/nlp`：專案自有資料，只當選用的 `route` 種子；文字偏模板，預設降權。
- 合成資料：由外部 LLM 依情境卡產生，prompt 不含真實日誌或個資；使用外部服務產物的條款以該服務為準。
- **不收他人資料**。艾斯維爾本人日誌（`real_owner`）只進 `pool_private/`（不入 git），`speaker_ref` 換成 `S1..Sn`。
- 測試集 = zh-TW 原句經 MT、人工核過，雙標後凍結；訓練工具永遠不讀 `testset/`。草稿內的「intended」只是出題意圖，不是真值。
