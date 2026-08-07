# Agent Memory Spike

Coding agent 記憶層實驗。與 `echo_memory/` 完全無關——不 import、不修改，只參考它的架構概念。
放在這個 repo 只是暫時寄居，成形後會獨立成專案。

零第三方依賴是硬性約束：hook 每輪都會跑，冷啟動延遲直接影響體感。

## 檔案

| 檔案 | 階段 | 用途 |
|---|---|---|
| `hook_session_start.py` | 0 | SessionStart hook，注入記憶 |
| `data/golden_memories.json` | 0 | 黃金資料，人工萃取自 PM notebook |
| `experiment/questions.md` | 0 | 對照題組與判定標準 |
| `experiment/results.md` | 0 | A/B 實驗結果 |
| `experiment/surprisal-calibration.md` | 0 | 自評 vs 行為測試的校準實驗 |
| `transcript.py` | 1 | transcript 解析 → episode |
| `hook_stop.py` | 1 | Stop hook，寫入 episode |
| `test_transcript.py` | 1 | 解析層測試（29 項） |
| `distill.py` | 1.5 | 語料 → concept 候選 → 蒸餾 |
| `calibrate.py` | 1.5 | surprisal 行為校準 + 注入實驗 |
| `experiment/phase15-results.md` | 1.5 | 蒸餾與校準結果 |
| `experiment/phase15-evaluation.md` | 1.5 | 完備性與可利用性驗收 |
| `retrieve.py` | 2 | 檢索（BM25 + ollama 向量 + 檔案訊號） |
| `experiment/phase2-retrieval.md` | 2 | 檢索驗證結果 |

資料一律放在 repo 外的 `~/.claude/agent-memory-spike/`——hook 全域掛載，
會收到所有專案的對話原文，包含商業專案。

---

# Phase 0 — kill switch（已完成）

驗證兩件事：注入記憶有沒有用、冷啟動能不能忍。

**按事前登記的標準沒有完全達標**，但產出了兩個比通過與否更重要的結論：

1. **記憶價值 = surprisal，不是 salience。** 通用最佳實踐模型本來就會，注入純屬浪費 context；
   價值最高的是「模型自信地相信錯誤的事」。從 Echo 繼承的 salience（重要性）在這裡衡量錯了軸。
2. **surprisal 不能用 LLM 自評測量。** 準確率約 60–70%，且在最有價值的條目上系統性失準——
   模型無法內省自己的錯誤信念。只能靠行為測試。

副產品：Dream Engine 在 coding 情境終於有明確職責——離線批次跑行為測試校準 surprisal，
且模型升級後要重跑（記憶價值會隨模型進步自然衰減）。

細節見 `experiment/` 底下三份文件。

---

# Phase 1 — 寫入管線（已完成）

**只寫入，不召回。** 目的是累積真實語料——surprisal 的行為測試需要真實使用情境才划算，
人工出題測人工資料只會測到出題品質。

## 為什麼不能用 `last_assistant_message`

Stop hook 的 payload 有 `last_assistant_message`，看起來可以直接用。不行。

實測本 repo 一個 session 的 transcript（Claude Code 2.1.216）：

```
373 行
  assistant 152  → tool_use 82、thinking 40、text 30
  user       87  → tool_result 81、真正的文字輸入僅 6 筆
  其餘為 metadata
```

**`user` 型記錄裡有 93% 不是使用者說的話。** 而一輪可能是幾十次 tool call 加最後一段結論，
`last_assistant_message` 只拿得到那段結論。

實測最極端的一輪：使用者輸入 9 字元，但該輪有 671 字元回覆、6 次 tool call、改了 1 個檔案。
只存 `last_assistant_message` 的話這輪幾乎完全消失。

所以自己讀 `transcript_path` 重建。

## origin：最要緊的正確性

非 tool_result 的 user 記錄有 6 筆，但只有 3 筆是使用者真的打字。其中一筆 3402 字元的是
**背景 agent 的 task-notification**。

如果天真地把所有 user 記錄當成「使用者說的話」，就會把 agent 自己的輸出偽裝成使用者的指示存進記憶。
這是嚴重的記憶污染，而且事後很難察覺。

`origin.kind` 是唯一能區分的欄位：

| 欄位組合 | 判定 |
|---|---|
| `promptSource='typed'` + `origin.kind='human'` | 使用者真的打的字 |
| `promptSource='system'` + `origin.kind='task-notification'` | 背景 agent 回報 |
| `isMeta=True` | 系統注入的 meta 訊息 |
| 三者皆無 | session 起始注入（CLAUDE.md / hook context） |

每輪都存，靠 `origin` 標記區分——不過濾，只標記。

## episode schema

`promptId` 是切輪的鍵（只有 user 記錄帶它，assistant 記錄靠位置歸屬）。

值得一提的欄位：

- `cwd` / `git_branch` 用 **list**：實測同一 session 內兩者都會變（切分支、bash 進子目錄），
  存單一值會失真
- `tool_sequence`：這輪實際做了什麼的骨架
- `files_edited` / `files_read`：取自 `tool_use` 的路徑參數，並補上
  `file-history-delta.trackingPath`。**兩個來源缺一不可**——delta 只涵蓋一部分編輯
  （語料裡 Edit 出現 5996 次，卻只有 409/1372 輪有 delta 記錄），
  而且它是 repo 相對路徑、`tool_use` 是絕對路徑，兩者都要正規化到 repo 相對才比對得起來。
  讀與改分開存：搜尋、確認、瀏覽都會讀檔，混進去會把「同一檔案被反覆修改」的訊號淹掉。
- `thinking_blocks` **只存數量**：內容是內部推理，體積大且無召回價值

## 存儲

每個 session 一個 jsonl，append 寫入。不同 session 落在不同檔案，
天然沒有跨程序寫入衝突——把鎖的問題留到真的要做跨 session 聚合時再解。

存放位置：`~/.claude/agent-memory-spike/episodes/`，**刻意放在 repo 外面**。

這個 hook 是全域掛載的，會收到所有專案的對話原文，包含商業專案。
放在 repo 內就算有 gitignore，仍有 `git add -f` 或規則變動而外洩的風險；
放在 `~/.claude` 底下則從根本上不可能被誤 commit。
跨專案集中存放是刻意的——Phase 2 要驗證的正是跨專案一致性。

（黃金資料則相反：它是人工萃取的實驗素材，有 gitignore 例外讓它留在版控裡。）

## 絕不寫入尚未結束的輪次

初版用 Stop hook payload 的 `prompt_id` 定位「當前輪」並寫入。**這是錯的**：
Stop hook 觸發時，該輪的記錄不保證已經完整寫進 transcript。

實測抓到的後果——某輪存進去時 `assistant_text` 只有 670 字元、5 次 tool call，
而該輪真正的內容是 2225 字元、14 次。少了七成，且因為「prompt_id 已記錄就跳過」
的去重邏輯，這筆殘缺資料**永遠不會被更新，也沒有任何欄位標示它不完整**。

所以改成：**每次觸發做一次增量同步，並排除最新的一輪**。
有下一輪開始 = 前一輪必定已結束，這個不變式保證每筆寫入都是完整的。
副作用是自我修復——某次 hook 失敗或撞上寫入中的 transcript，下次會自動補上。

## 用法

```bash
# 模擬 Stop hook（不需要 prompt_id）
echo '{"session_id":"...","transcript_path":"..."}' | python hook_stop.py

# 手動同步一份 transcript
python hook_stop.py --sync <transcript_path>

# 健檢：比對所有存檔與來源 transcript，並報告覆蓋度（唯讀，不修改）
python hook_stop.py --doctor

# 全量重建，修復殘缺紀錄
python hook_stop.py --repair <transcript_path>

# 掃過所有 transcript 補齊遺漏（session 中斷時尾端會漏，這支收尾）
python hook_stop.py --sync-all

# 對所有既有 session 全量重建 —— schema 變更後必跑
# --sync-all 只補「沒記錄過」的輪次，對既有紀錄完全不動，
# 所以欄位一改，舊語料會永遠停在舊格式且沒有任何標示
python hook_stop.py --repair-all

# 只解析不寫入
python hook_stop.py --sync <transcript_path> --dry-run
```

動語料前先備份 `~/.claude/agent-memory-spike/episodes/`——`--repair-all` 是不可逆的。

## 量測（2026-07-25，Windows 11）

10 次平均 wall clock：

| hook | 延遲 |
|---|---|
| SessionStart（Phase 0） | 117 ms |
| Stop（Phase 1，解析 1.45MB transcript） | 132 ms |

差距只有 15 ms——**解析 1.45MB 的 transcript 只花 15 毫秒**，主要成本仍是 Python interpreter 啟動。

## 已知限制

- **episode 永遠落後一輪**，且 session 的最後一輪要等下次 resume 才補得到。
  這是排除最新輪的必然代價。若缺漏累積太多，可考慮加掛 SessionEnd hook 補收尾
- **每次觸發都重讀整份 transcript**，單次 O(n)、整個 session O(n²)。
  1.45MB 時只有 15ms 所以先不優化，但長 session 會惡化
- **去重要讀完整個 episode 檔**（約 20KB/輪，百輪級約 2MB）。
  早期版本只比對最後一筆，批次寫入時整份重複寫入，實測抓到後改成讀全部
- **`user_text` / `assistant_text` 是原文**，可能含機敏內容。目前純本機、不會被召回注入，
  風險可控；Phase 2 要把內容送回 context 前必須先過一次消毒
- **失敗全部是靜默的**（一律 exit 0，不阻斷 session）。這是刻意的，
  但代價是壞掉不會有人通知你——累積期間要定期跑 `--repair` 當健檢

## 測試

```bash
../U.E.P-s-Core/env/Scripts/python.exe -m pytest agent_memory_spike/test_transcript.py -q
```

不放在主專案 `tests/` 底下，避免混進 echo_memory 的 suite。

---

## Claude Code hook 限制（查證自官方文件）

- `SessionStart` / `Setup` / `UserPromptSubmit` 可用**純 stdout** 注入；
  其他事件必須用 JSON 的 `hookSpecificOutput.additionalContext`（放最外層會被靜默忽略）
- 注入上限 **10,000 字元**，超過會轉存成檔案改傳路徑
- `SessionStart` 觸發時 **MCP server 通常還沒連上**，只能用 `type: command`
- `Stop` 有 `last_assistant_message`，`SessionEnd` 沒有
- `PreCompact` **拿不到即將被壓縮的內容**
- Timeout：一般 600s，`UserPromptSubmit` 僅 30s；逾時 = 忽略，不中斷

## Phase 1 的下一步（已完成）

- [x] 累積 2–3 週真實語料（1372 輪 / 7 repo）
- [x] 依真實語料做 surprisal 行為測試校準（見 Phase 1.5）
- [x] Phase 2：檢索核心（見下）

⚠️ **`hook_session_start.py` 至今刻意未掛載。** 現在注入記憶的話，
之後收到的語料會是「已被記憶影響過的行為」，校準就會有系統性偏誤。
真正要接上去之前不要掛。

---

# Phase 1.5 — concept 蒸餾與 surprisal 校準（已完成）

第一輪：1372 輪語料 → 粗篩 78 組 → 蒸餾 55 條 concept → 全數行為校準，22 條通過門檻。

第二輪（全語料）：1411 輪 → **561 組相鄰 human 輪對** → 780 條 → 去重 **775 條**（尚未校準）。

改跑全語料的理由是粗篩訊號實測**沒有鑑別力**（候選組 0.705 條/組 vs 隨機對照 0.692），
而它只涵蓋約 16% 的語料。篩選既然無效，剩下的選擇就只有全跑或接受低覆蓋。

全跑最大的收穫不是條數，是**解鎖了粗篩在結構上就撈不到的類型**：
`user-stance` 從 1 條變成 41 條。原因很直接——**表達立場的輪次通常沒有檔案被改**，
「同一檔案被連續修改」是技術除錯的結構特徵，不是意見分歧的。
而按 Phase 0 的結論，user-stance 恰恰是價值最高的一類。

```bash
python distill.py --stats            # 看粗篩分布
python distill.py --emit             # 產蒸餾任務（只跑粗篩候選）
python distill.py --emit --all       # 產蒸餾任務（全語料，561 組）
python distill.py --show 0-12        # 取一批任務（給 subagent）
python distill.py --ingest <dir>     # 收回蒸餾結果

python calibrate.py --emit           # 產行為測試題目
python calibrate.py --show-probes 0-5    # 取題（**只給題目，不含答案**）
python calibrate.py --ingest <dir>   # 收回判定，計算 surprisal
python calibrate.py --emit-injection # 注入實驗（可利用性驗證）
```

**方法上的三條紅線**（違反任何一條，測出來的數字就沒有意義）：

- 受測者**絕不能看到 statement**——看到就退化成自評，而自評準確率只有 60–70%，
  且在最有價值的條目上系統性失準
- probe 的框架要偽裝成**正常開發諮詢**，不能明說是測驗——講明了模型會進入應試狀態，
  把所有想得到的注意事項列一遍，人為抬高 VOLUNTEER 率
- 受測用的模型要與**實際會用的模型一致**（目前 opus）；別的模型測出的先驗代表不了它

驗收結論見 `experiment/phase15-evaluation.md`：
可利用性過關（注入後 22/22 APPLIED、零誤用），但**粗篩訊號無鑑別力**
（對照隨機抽樣：0.705 vs 0.692 條/組），語料覆蓋率僅約 16%。

# Phase 2 — 檢索（進行中）

```bash
python retrieve.py --eval              # recall@k / MRR，比較 8 種設定
python retrieve.py --eval-files        # 「同檔案再次編輯」情境下的檔案訊號
python retrieve.py --query "..."       # 手動查一筆
python retrieve.py --no-vector         # 不呼叫 ollama，只跑 BM25
```

embedding 走 **ollama HTTP**（`bge-m3`，與 PM 同一個模型），不自載模型：
`UserPromptSubmit` 只有 30 秒 timeout 且每次新程序，自載等於每輪付一次冷啟動。
ollama 常駐 → 一次往返 371 ms，**也就不需要自己做 daemon**。
只用標準庫 urllib，零依賴前提保住。

**核心結論**：索引「提取線索」比換檢索演算法有效得多
（換向量 ±0.0pp，換索引內容 +8~10pp）。這正是 Ecphory 的意思——
記憶靠 cue 被喚起，不是靠內容被搜到。

詳見 `experiment/phase2-retrieval.md`。

## 第二輪：775 條池子上的複驗

`cue` 正式驗證通過（先前是借 `probe` 當代理）。recall@5：

| 設定 | 無 cue | 有 cue |
|---|---|---|
| BM25 + scope | 22.9% | **28.8%** |
| 向量 + scope | 23.4% | 25.6% |
| Hybrid + scope | 26.7% | **31.9%** |

「換演算法沒用」再次重現：BM25 18.1% vs 向量 18.7%（差 0.6pp），
而換索引內容差 5.2pp。

**絕對數字從 69.4% 掉到 31.9%，那是預期的**：池子從 73 條變 793 條、
query 從 62 個變 659 個，兩者不可比。上輪已知限制第一條寫的
「73 條是樂觀估計」現在量到了，31.9% 才接近真實部署規模。

**`anchors` 仍未驗證，因為測法不成立**：ground truth 用 `source_files` 定義、
檢索用 `anchors`，兩邊脫鉤；更根本的是 anchors 現在含函式名與欄位名，
而 episode **只存了檔案路徑**——符號類錨點在集合交集下永遠不可能命中。
要求蒸餾者把粒度做細，卻沒同步改比對的另一邊，細粒度在這個測法下純扣分。
要真正驗證它，得先讓 `transcript.py` 存下 Edit 的內容片段（PreToolUse
實際拿得到 `old_string`/`new_string`，語料裡卻沒有），那需要改 schema + `--repair-all`。

## 路徑正規化的基準點會浮動（已修）

`normalize_path` 切掉的是「往上最近的 `.git`」，但實測 AI-Website 是 **nested git repos**，
加上 bash 會切目錄，於是**同一個檔案有三種表示**：

```
cwd=mind-door/AI-Website   → AI-Website-API/src/routes/compliance-v2/types.ts
cwd=.../AI-Website-Web     → C:/Users/.../AI-Website-API/src/...（完全沒切）
cwd=.../AI-Website-API     → src/routes/compliance-v2/types.ts
```

`files_edited` 有 **11.8% 是未能正規化的絕對路徑**，波及 183/1411 輪。
而「同一檔案被反覆修改」是粗篩訊號與檔案訊號**共同的基礎**。

修法是 `transcript.file_key()`：取路徑末 3 段小寫當比對鍵，**只在比對時收斂、不寫回語料**
（schema 一動就要 `--repair-all`，而 `repo`/`scope` 從同一個 root 推導，
動了它們，既有 concept 的 scope 會整批對不上）。

效果：粗篩候選 80 → 91 組，檔案情境 case 554 → 684，檔案訊號 recall@5 12.5% → 14.6%。
**但影響比預期小**——候選只多 11 組，不足以推翻「粗篩訊號無鑑別力」的結論。

## 已知待處理

- **記憶會被後續語料推翻**：四批蒸餾者各自撞到「這條事實在後面幾輪就被改掉了」。
  能自救的都是因為衝突剛好落在同一批，**跨批的一律漏網**。去重只比字串，抓不到矛盾
- **字串去重幾乎無效**：780 → 775 只掉 5 條，語意重複仍大量存在
- **蒸餾判準的評審間變異 2.2 倍**：各批產出密度 0.83 ~ 1.96 條/組
- **語料可能有重複**：cand-341 與 cand-353 內容完全相同且相隔 12 組，
  不是滑動窗口造成。若 resume 讓同一輪換了新 `prompt_id`，
  `(prompt_id, turn_index)` 去重就擋不住
- **空 `assistant_text` 的輪次**：多批回報撞到，`--doctor` 尚未把它列為檢查項
