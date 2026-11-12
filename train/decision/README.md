# train/decision — Laya 決策模型訓練資料工具

> 設計：[`hidden_docs/架構重構/決策模型訓練設計草案.md`](../../hidden_docs/架構重構/決策模型訓練設計草案.md)（草案，待裁）；標註規格：[`決策標註規格草案.md`](../../hidden_docs/架構重構/決策標註規格草案.md)。
> 這裡是**純資料工具**，不改 production；訓練資料工具不 import `uep`，評估端工具（`build_en_test`、`eval_testset`）需要載入 `uep` 的翻譯器與規則版 proposer。**Laya 只用英文訓練**；題目與 state 值一律英文。

## 目錄

```
train/decision/
├── laya_data/
│   ├── kinds.py        # 每個決策 kind 一份 KindSpec（題目、state builder、expected 驗證）
│   ├── convert_nlp.py  # train/nlp -> laya-train JSONL（選用種子，只產 route）
│   ├── synth.py        # 情境卡合成器（卡片決定標籤，LLM 只生文字）
│   ├── split.py        # train/val 切分、測試集防洩漏檢查
│   ├── resolve.py      # 盲標 disputed 規則化裁決、卡片重標、最終訓練集（final／train／val／stats）
│   ├── compound.py     # 既有資料的 compound 例外清單（預設 no）與定義
│   ├── build_en_test.py  # 評估端：測試集 zh-TW 照線上流程翻成英文（testset/en_test.jsonl，不入版控）
│   ├── overlap.py        # 評估端：測試集與 train／val 的逐列重疊報告（含 state 層級）
│   └── eval_testset.py   # 評估端：Laya checkpoint（predict）、rules.default（rules）、指標與錯誤分析（report）
├── testset/zh_candidates_draft.jsonl   # 測試集 zh-TW 原句草稿（status=pending_review，待艾斯維爾核）
├── testset/en_reference.jsonl          # 84 筆中文測試集的人工參考英譯（id／text_en／context_en；build_en_test --reference 用）
├── testset/en_independent.jsonl        # 獨立英文測試集（231 筆，只量 Laya 本身；meta.pending_review=true 的列待艾斯維爾核，評分可排除）
├── testset/capability_pairs.jsonl      # 能力清單成對評測題（21 組、每組 2 題，同句只換 available_capabilities）：A 類私聊直接對她說，route／reply_mode 兩題相同（route 照意圖分，高風險 defer／task_request）；B 類群組未點名且前文不是對她說，有該能力 passive、沒有 pass（addressed=False、route=ignore）
├── pool/           # synthetic 內容池（入庫；量產後才有）
├── pool_private/   # real_owner 日誌（.gitignore，不入 git）
└── out/            # 產物（.gitignore）
```

turn.reply 的題目與 state 取自 `uep/decision/laya_schema.py`（訓練與推論共用，不另寫）；助理名一律用 MT 佔位名 `Yuki`，測試集 zh 草稿中的「U.E.P」在 MT 後會成為 Yuki。

已實作 kind：`turn.reply`（reply_mode／route／addressed，沿用既有 adapter 題目 ID）、`presence.absent_intent`（`absent.intent`／`absent.target_location`）。新增 kind＝在 `kinds.py` 加一份 `KindSpec` 與 `synth.py` 的情境卡。

## presence.absent_intent 的四類

- `presence_request`：要她過來／移動到某處（`target_location` 只有此類可非 none）。
- `message`：純內容、當下可完成：轉告他人（無時間／事件觸發）、要她記住的內容（`remember that…`）。
- `task`：要她執行動作：提醒／排程（`remind me`、`ping me`、`don't let me forget`，沒說時間也算）、轉告但帶時間／事件觸發（`tell Anna at five…`）、不在場時交辦的其他動作（`book the table`）。
- `other`：閒聊、提問、否定、過去的事、純自我筆記（`I need to remember to…`）、不明。
- `compound`（turn.reply）／`absent.compound`（absent_intent）：`noul` 是／否題。符合這個定義就標 yes：一句裡有好幾個彼此獨立、各自交給不同處理路線或對象的意圖（`remind me at four…, and tell Rob…`、閒聊＋交辦、去某處＋轉告只是例子，不是限定）。no＝同一目標的多步驟（agent_loop）、一個意圖多項內容（`tell Rob I'm late and sorry`、`remind me to buy milk and eggs`）、段落間有條件或資料依賴（`check the forecast and if sunny tell Rob`；在場走 agent_loop，不在場照意圖標，多為 task）。`request_two_places` 維持 yes。
- compound=yes 時其餘欄位照退回優先序（拆分失敗時的答案）：absent_intent `presence_request > task > message > other`；turn.reply 的 route `agent_loop > task_request > direct_chat`，reply_mode 只要有一段對她說就 respond，addressed 同理（只要有一段對她說就 True，例：「Yuki 幫我訂位，Rob 你去買飲料」→ True）。absent 的 `request_two_places`（去兩個地方）退回時 `target_location` 填第一個字面地點，其餘留到拆分時處理。
- 既有資料預設 compound=no，例外見 `compound.py`；候選句（含連接詞＋第二個動作）另經獨立盲標抽驗。新樣本走 `--card-set compound`（正例）與 `--card-set single`（反例）。
- **ack_after_statement 裁決（艾斯維爾）**：她剛說完一句陳述、使用者回裸附和（好的、嗯嗯、ok），這句是對她的回應，`addressed=True`；她不必回話，`reply_mode=pass`、`route=ignore`，只用來進一步確認先前的決策。前提是接在她的發言之後；沒有前文的冷啟動附和（`ack_cold_start`）維持 `addressed=False`。訓練卡片 `ack_after_statement` 與測試集都照此標；盲標當時依舊規則標 False，以裁決為準（`resolve.py` 規則 `ack_ruling`）。
- **passive 界線（艾斯維爾，11/12）**：passive 代表她有具體理由開口，至少符合一條，否則一律 pass；群組裡拿不準時往 pass 靠（該 pass 卻插話的代價高於錯過一次幫忙）。判斷時問：她插話，在場的人會覺得幫上忙還是被打斷？
  1. 跟她答應過、記下或說過的事有關，而且這件事現在還在進行或正要被使用（例：她給的資訊被當場指出可能有誤、使用者正要照著做 → passive；她過去出過錯但使用者已自行核對 → pass）。
  2. 沒點名的提問，而且她答得出來。
  3. 沒點名的請求，而且她確實做得到。
  - 第三人稱提到她做過的事（稱讚、評論、回憶）、旁人之間的陳述、轉述別人的消息、不帶她能回答的問題的自言自語 → pass。
  - 「值得記下來」（例：航班改到週四）屬記憶功能，不進 reply_mode。
  - **能力依據**：她的能力只限電腦與各平台內的操作（查詢、訂位訂場、檔案與程式操作等），另加打電話、傳訊息、寄信等通訊；判斷時以訓練資料實際出現過的為準。電腦與平台以外的操作一律視為做不到，包括實體裝置控制（燈光、烤箱、窗戶等）與現場感知（看路標、看印表機墨水量等）。新增能力時，回頭檢查依此判為 pass 的題目。
- **passive 收窄實作**：既有 passive 逐筆重審的結果在 `resolve.PASSIVE_NARROW`（`label_rule=passive_narrow`）；沒點名的翻譯請求等訓練資料沒出現過的能力改標 pass 見 `resolve.PASSIVE_CAPABILITY`（`label_rule=passive_capability`）。判準本身以上面「passive 界線」為準。
- **能力清單（艾斯維爾裁決，state.available_capabilities）**：她做得到什麼是模型的輸入，不是標註規則。`uep/decision/laya_schema.py` 定義 9 個能力類別（query／booking／messaging／calling／files／media_control／timer_alarm／host_control／device_control）與現行預設清單（不含 device_control），turn.reply 的 state 帶這個欄位；工具層清單接上後由組裝層傳給 `LayaProposer(capabilities=...)`。標註依清單條件判斷：有對應能力→對她的請求 respond／task_request（群組未點名依 passive 第 3 條）；沒有→對她說的 respond／direct_chat（回答做不到），群組裡未點名且無前文的 pass。`capabilities.py` 負責：預設清單、實體裝置請求與其他能力類別的成對樣本（同一句配有／沒有該能力）、少量隨機清單（只做不影響標籤的變動）、她前文提議實體操作者補上 device_control；盲標審核後的人工紀錄在 `capability_overrides.json`。測試集沒有這個欄位，評估端 `predict --schema current` 補預設清單並取目前的題目說明。
- **r4 補強卡組**（`--card-set r4_passive｜r4_pass｜r4_side｜r4_defer｜r4_ack｜r4_mt｜r4_info`）：passive 三條正例、看似 passive 的 pass 反例、群組對旁人說的祈使句、defer（晚點再說／資訊不足／有風險）、ack、passive 第 1 條的資訊對照（r4_info：她給的資訊正要被使用＝passive，已核對／已結束＝pass）、貼近 MT 的雜訊說法；sample_id 前綴 `r4-`。生成走 `--export-prompts`／`--import-responses`，新列先過 `laya_data.leak_filter`（與 `en_test`、`en_reference`、`en_independent` 比對，近似重複者剔除，只輸出 id 與分數），再盲標（盲標員另帶上面的 passive 判準）。r4 列 reply_mode 為 pass／passive 時 route 一律視為 ignore（`resolve.normalize_r4_labels`），只比對 reply_mode 與 addressed。
- 評估比較：`python -m laya_data.eval_testset compare --set NAME DATA R1_GLOB R2_GLOB [RULES]`，各欄位分列、passive／pass／defer 的 precision 與 recall、混淆矩陣與指定題逐題結果。
- 一句多個指示先取第一個；`task` 與 `message` 的文字邊界由 `resolve.py` 的 `relay_vs_task` 規則判定，並須與盲標一致。
- 對照卡（`synth.ABSENT_CONTRAST_CARDS`，`--card-set contrast`）另成一組，避免動到主卡組的 plan 而無法重現；卡片標籤改版後直接重跑 `python -m laya_data.resolve --mass-dir out/mass`（會依目前卡片重標 `synth_all.jsonl`、重算 accepted／disputed）。

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

## 測試集出題規則（zh_candidates_draft.jsonl）

完整判準見標註規格草案 §8.4、§9；摘要：

- `route` 依副作用：`direct_chat`＝閒聊＋唯讀查詢（天氣、新聞、查行事曆、記憶查詢）；`task_request`＝單一明確且改變狀態的動作（寄信、設提醒、開關裝置、訂位）；`agent_loop`＝多步驟／需規劃／串接多次工具。「你可以 X 嗎？」是禮貌請求；ack 的 route 跟被確認的動作走。
- `absent.target_location` 只記字面明說的地點（「過來」＝`none`）；absent 句中「她」指 U.E.P，第三人寫名字；叫她去別的平台用「去」，用「來」時 `source_platform` 為說話者所在平台。
- 每列有 `speaker`：`same_as_context_user`／`other_user`／`n/a`（無 context），用來判斷群組 follow-up；`others`（在場他人數）涵蓋 0／1／2／3+；`passive`、`defer` 各至少 6 句。
- 新增列一律 `status=pending_review`；意圖只是出題參考，不是真值。
