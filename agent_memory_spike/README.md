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
| `retrieve.py` | 2 | 檢索（BM25 + ollama 向量 + 檔案／符號訊號） |
| `experiment/phase2-retrieval.md` | 2 | 檢索驗證結果 |
| `consolidate.py` | 2.5 | 池子收斂（語意去重 + 矛盾偵測 + 關係閉包 + 雙評審合議） |
| `hook_pretooluse.py` | 3 | PreToolUse hook，編輯前注入相關記憶（**已全域掛載**） |
| `pipeline.py` | 3 | 自動化管線（收料 → 蒸餾 → 收斂 → 校準） |
| `test_consolidate.py` | 2.8 | 關係閉包與雙評審合議測試（11 項） |
| `test_inject.py` | 3 | 注入 hook 測試（10 項） |
| `test_pipeline.py` | 3 | 管線機制測試（11 項） |

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

---

# Phase 2.5 — 池子收斂與符號級錨點（已完成）

```bash
python consolidate.py --pairs        # 算相似對（同 scope 內，餘弦 ≥ 0.80）
python consolidate.py --show 0-39    # 取一批配對（給判定者）
python consolidate.py --ingest <dir> # 收回判定並套用
```

## 語意去重 + 矛盾偵測

兩件事的第一步相同（找語意相近的 concept 對），所以合成一支工具，
只在 LLM 判斷時問不同的問題。實測 775 條 → 237 組配對：

```
DUPLICATE      178 組     移除 156 條
CONTRADICTION    8 組     移除   7 條
DISTINCT        51 組
                          775 → 612 條
```

**8 組真矛盾**證實「記憶被後續語料推翻」不是理論擔憂。最清楚的一組：
`compliance-v2` 的 `tracking.ts` 早期完全信任前端傳的 `user_departments`，
後來改成後端用 JWT 自己查——**舊那條留著被召回，會讓人以為後端沒驗證**。

池子縮了 21%，檢索反而變好（recall@5 31.9% → 33.1%，MRR 0.211 → 0.236）：
移除的確實是雜訊——重複條目互相稀釋排名，過期條目佔位。

⚠️ **矛盾有傳遞性，單輪配對處理不完**：實測 `c-138`/`c-617` 被判與 `c-711` 重複，
而 `c-711` 又被 `c-238` 推翻——那兩條其實也過期了，卻因為沒被直接配對到而逃過。
需要多輪迭代或叢集處理。

## 符號級錨點

`anchors` 的粒度做到了函式名與欄位名，但 episode 只存檔案路徑，
比對是集合交集——**符號級錨點永遠不可能命中**。
現在從 `Edit`/`Write`/`MultiEdit` 的參數抽識別符存進 `symbols_edited`
（只存符號不存原文：內容體積大且含機敏資訊，而比對只需要符號）。

同池同 case 對照（623 個 case、630 條池）：

| 錨點 | recall@1 | recall@5 | MRR |
|---|---|---|---|
| 只用檔案 | **12.2%** | 16.9% | 0.143 |
| 檔案 + 符號 | 8.5% | **23.6%** | **0.152** |

符號把更多正確答案帶進 top-5，但會擠掉一部分第一名。MRR 淨上升。

**權重是實測選的**：給 2 時 MRR 只有 0.114，自作聰明加「罕見符號權重更高」的
稀有度衰減是 0.136，最單純的等權 0.152 最好。假設被推翻就不留那段複雜度。

## 跨 session 去重的鍵原本是錯的

原本用 `(prompt_id, turn_index)`，理由是「resume 的完整複本序號一致」——
**那個假設是錯的**，實測同一輪在兩個 session 檔裡分別是 `turn_index` 2 和 3，
32 組、64 輪（4.5%）就這樣重複進了語料。

改成先按 `(prompt_id, user_text 指紋)` 分桶，桶內再用 `assistant_text` 的
**前綴關係**判斷是否同一輪：殘缺的副本必然是完整版的前綴，
真正不同的兩輪則從頭就不一樣。寫入端的鍵維持不變（單一檔內序號不會位移）。

## 增量蒸餾

```bash
python distill.py --emit --all --incremental   # 跳過已蒸餾過的組
python distill.py --ingest <dir> --incremental # 接在既有 concept 之後
```

`cand-NNN` 是按位置編號的，語料一長就位移，而 `--ingest` 靠 id 對回 task 拿溯源——
位移後會把 A 組的 concept 掛到 B 組的來源輪次上，**且完全靜默**。
改由 `source_turns` 派生 sha1，配上 `distilled.json` watermark。
這是接定期自動蒸餾的先決條件。

## 已知待處理

- ~~**矛盾的傳遞性**~~：已處理，見 Phase 2.8——實際規模遠小於預期，
  真正的漏洞是判定的系統性保守
- **蒸餾判準的評審間變異 2.2 倍**：各批產出密度 0.83 ~ 1.96 條/組
- ~~**612 條尚未校準**~~：高價值的 158 條已校準（見 Phase 2.7），
  `project-fact` 那 454 條仍未測
- ~~**空 `assistant_text` 的輪次**~~：已查根因並補進 `--doctor`，見下節
- **自動化尚未接上**：穩定 id 與增量都到位了，但排程入口、lockfile、
  每次上限、以及「蒸餾 → 校準 → 入池」整條 pipeline 還沒串

---

# Phase 2.6 — 空 `assistant_text` 的根因與 doctor 補強（已完成）

語料裡 11.4% 的輪次 `assistant_text` 是空的，而 `--doctor` 完全不看這個欄位。
逐筆回對 transcript 之後，成因分成四類：

| 類別 | 輪數 | 是不是故障 |
|---|---|---|
| 無回應（送出後立刻中斷／訊息排隊） | 135 | 否，agent 本來就沒回應 |
| 無法驗證（transcript 已被 cleanup 清掉） | 25 | 未知 |
| 中斷於工具執行中（有 tool_use、無文字結論） | 8 | 否 |
| **殘留（來源有回應、存檔卻是空的）** | **0** | **是** |

**沒有解析漏抓**。一度以為有 2 筆，那是分析腳本用 `{promptId: group}` 建索引造成的假象——
**同一個 promptId 在一份 transcript 裡會出現兩次**（session 起始的 meta 注入沿用同一個 id），
dict 後寫者贏，於是拿 turn 0 的空 meta 輪去對照 turn 33 的 human 輪。
語料的鍵本來就是 `(prompt_id, turn_index)`，沒有問題；出錯的是臨時腳本。

## 修掉的盲點比補的檢查更重要

比對用的 `live` 來自 `completed_episodes`（排除最新一輪），
所以**transcript 只有這一輪時 `ref` 是 `None`，整筆被靜默略過**。
空 `assistant_text` 的檢查改成對照含最新輪的完整清單，這類輪次才進得了視野。

順帶：原本每個 session 會重建三次 transcript，現在一次。

`--doctor` 現在會印出空 `assistant_text` 的分類統計，只有「殘留」那類算問題。

---

# Phase 2.7 — 高價值子集校準（已完成）

612 條全跑的成本是一次蒸餾的量級以上，所以先跑
`user-stance` + `belief-correction` 共 158 條（`--kinds` 新增於此）：

```bash
python calibrate.py --emit --kinds user-stance,belief-correction --probe-path <path>
python calibrate.py --show-probes 0-12 --probe-path <path>   # 受測（13 個乾淨 agent）
python calibrate.py --show-judge 0-25 --answer-path <dir>    # 判卷（8 個 agent）
python calibrate.py --ingest <verdicts_dir>
```

| kind | 條數 | VOLUNTEER | PARTIAL | SILENT | CONTRARY | 通過 |
|---|---|---|---|---|---|---|
| belief-correction | 121 | 42 | 28 | 27 | 24 | 51（42.1%） |
| user-stance | 37 | 11 | 7 | 7 | 12 | **19（51.4%）** |
| 合計 | 158 | 53 | 35 | 34 | 36 | 70（44.3%） |

## user-stance 的高價值第一次拿到實測支持

Phase 0 憑黃金資料判斷 user-stance 最有價值，那批是人工挑的。
這次在真實語料上量到同一個結果：通過率 51.4% > 42.1%，
而**最高價值的 CONTRARY 佔比 32% vs 20%**——模型不只是不知道使用者的立場，
是會主動提出相反的做法。

這也回頭確認了全語料蒸餾的價值不在條數：粗篩訊號按定義漏掉 user-stance
（表達立場的輪次通常沒有檔案被改），而那正是密度最高的一類。

## 原子化見效：PARTIAL 從 42% 降到 22%

Phase 1.5 有 42% 的判定落在 PARTIAL——一條 statement 綁了兩件事，
一件模型知道、一件不知道，收斂成無法決策的中間值。
蒸餾指示加了原子性要求之後，這輪是 22%（35/158）。

## `scope=TestSeperateMemorySystem` 的污染不再是「疑慮」

那批 6 條通過 1 條，VOLUNTEER 5 條——通過率 17%，遠低於其他 scope 的 38–67%。
受測 agent 在本 repo 目錄下跑會自動吃到 `CLAUDE.md`，
所以它「本來就知道」。**這批條目的判定不可信，之後要換乾淨目錄重測。**

## 已知限制

- **454 條 `project-fact` 仍未校準**。「它們的 surprisal 多半較低」是猜測，
  沒有數據——未校準的那批只能當「還沒測」，不能當「已知低價值」
- 判卷仍是單一評審，跨評審一致性未量
- 這輪沒跑注入實驗（可利用性）；Phase 1.5 的 22/22 APPLIED 是在 55 條的池子上測的

---

# Phase 2.8 — 矛盾的傳遞性（已完成，但結論不是原本以為的那個）

```bash
python consolidate.py --transitive <verdicts_dir>          # 只報告
python consolidate.py --transitive <verdicts_dir> --apply  # 才寫入
```

`--transitive` 不叫 LLM，純粹補兩個閉包：**DUPLICATE 是等價關係**（A≡B、B≡C ⇒ A≡C，
等價類裡只該留一條），以及**矛盾沿等價類傳播**（B 被 C 推翻、A≡B ⇒ A 也過期）。
同一等價類內部互相矛盾時只報告不處理——那代表判定自相牴觸，刪任何一邊都可能是錯的。

## 傳遞性是真的，但規模只有 0.3%

Phase 2.5 舉的那個例子（`c-138`/`c-617` ≡ `c-711`，而 `c-711` 被 `c-238` 推翻）
**實際上單輪就處理掉了**——那三條各自在別組配對裡當了輸家，早就不在池子裡。
閉包在 612 條上只多抓到 2 條等價類內重複存活、0 條矛盾傳播。

迭代也已經收斂：在收斂後的池子上重算配對，34 組裡只有 2 組沒判過，兩組都是 DISTINCT。
`MAX_PAIRS_PER_CONCEPT=4` 沒有藏住東西。

## 🚨 真正的漏洞是判定的系統性保守，不是傳遞性

重算配對時有 32 組是重判，正好拿到第一次的**評審間一致性：81%（26/32）**。
關鍵不在數字，在於**6 組不一致全部是同一個方向**：

```
第一輪 DISTINCT → 第二輪 DUPLICATE     5 組
第一輪 DISTINCT → 第二輪 CONTRADICTION 1 組
反方向                                  0 組
```

這不是隨機噪音。判定準則寫著「不確定就給 DISTINCT」，
於是 **DISTINCT 是安全答案，重複與過期被系統性低估**。
派兩個獨立評審做三票多數決，8 組爭議裡 5 組定案要處理（4 重複、1 過期），
其中 `c-504`/`c-559` 二比二平手——按紀律保留。

**含意**：想提高收斂品質，該投資的是「矛盾用雙評審」而不是「多跑幾輪閉包」。
矛盾比重複嚴重（過期的記憶是錯的，重複只是浪費 context），值得付雙倍判定成本。

→ **已照做，見 Phase 3.5**：284 組撈出 13 組真矛盾，其中一條 surprisal 0.8、
已經在可用池裡，而且是靠仲裁才撈出來的。

## 相似度門檻 0.80 確實會漏，但降門檻不划算

抽樣 30 組 0.72–0.80 區間的配對：DISTINCT 28、DUPLICATE 1、CONTRADICTION 1，
**命中率 6.7%**。該區間共 319 組，推估藏著約 21 組真配對——其中確實有真矛盾
（`c-123`/`c-623`，相似度 0.766，講同一個函式的使用範圍互相衝突）。

門檻上的命中率是 78%（186/237）。要多撈那 21 組得多判 319 組，
**除非只針對矛盾**——那才是值得降門檻的部分。

→ **已照做，見 Phase 3.5**。實際判了 284 組（扣掉已判過的），撈出 13 組真矛盾，
比這裡推估的 21 組「真配對」少——因為那 21 組的推估含重複，而這輪只問矛盾。

池子：612 → 610（閉包）→ 605（仲裁定案）。校準通過數不變（70 條）。

---

# Phase 3 — 接入前的驗證（進行中）

## A1：檔案訊號的 precision（已完成）

從 Phase 2 欠到現在的那一項。**recall 決定召不召得到，precision 決定召到的東西會不會誤導**，
而後者才是 `PreToolUse` 能不能掛的關鍵。

```bash
python retrieve.py --dump-precision <path> --sample 50 --top-k 5
python retrieve.py --show-precision 0-12 --precision-path <path>   # 給判定者
python retrieve.py --ingest-precision <dir> --precision-path <path>
```

50 個情境、236 條召回判定（605 條池子、anchors + 符號級錨點）：

```
逐條 precision（嚴格）    72/236 = 30.5%
逐條 precision（含 MARGINAL） 136/236 = 57.6%
情境命中率（至少一條 RELEVANT） 36/50 = 72.0%
```

### 🔑 只重疊一項的召回幾乎全是雜訊

| 重疊數 | 條數 | RELEVANT | IRRELEVANT |
|---|---|---|---|
| 1 | 61 | **8.2%** | **77.0%** |
| 2 | 114 | 36.0% | 35.1% |
| 3 | 54 | 42.6% | 22.2% |
| 4 | 7 | 42.9% | 14.3% |

「剛好碰到同一個檔案」不構成相關。設定因此定為 **top-3 + overlap ≥ 2**
（`INJECT_TOP_K` / `MIN_FILE_OVERLAP`）：

| 設定 | precision | 情境命中率 | 平均條/次 |
|---|---|---|---|
| top-3 / overlap≥1 | 38.2% | 66% | 2.88 |
| **top-3 / overlap≥2** | **43.5%** | **62%** | **2.30** |
| top-3 / overlap≥3 | 49.1% | 32% | 1.06 |
| top-5 / overlap≥2 | 38.3% | 68% | 3.50 |

門檻收到 3 就崩了——命中率腰斬，因為多數真正有用的召回本來就只重疊兩項。

**評測時不套門檻**，只在注入時套：寫死在 ranker 裡，門檻本身就量不出來了。

## A2：`injected` 欄位（已完成）

hook 遲遲不掛的唯一理由是「注入之後語料就變成已被記憶影響過的行為」。
但**問題不在注入，在於分不出哪些輪次被影響過**——標記起來就解開了，
而且這比實驗室注入實驗更接近真實效果。

- 注入 hook 自己寫 side-car `injections.jsonl`（`{session_id, prompt_fingerprint, injected}`），
  **不從 transcript 反推** `additionalContext` 的形狀——那沒有保證，靠猜會得到一個
  看起來正常但對不上的欄位
- 只存 prompt 指紋不存原文：這份檔案只用來比對，原文已經在 episode 裡了
- 空 list 與缺欄位分得開：前者是「這輪沒被注入」，後者是「早於這個 schema」
- `--doctor` 對帳：注入紀錄 N 筆但語料只對上 M 輪 → 報問題（指紋比對失效的話，
  被污染的輪次會被當成乾淨語料）。來源已消失而補不了的舊 schema 只統計不報錯

已跑 `--repair-all`：1374 輪重建，105 輪因 transcript 已被 cleanup 而停在舊格式。

## D1：project-fact 抽樣校準（已完成，全跑待決）

448 條全跑要五十幾個 agent，而「project-fact 的 surprisal 多半較低」一直只是猜測。
先抽 60 條（`--sample`），花 1/7 的成本把猜測換成數據：

| kind | 條數 | VOLUNTEER | PARTIAL | SILENT | CONTRARY | 通過率 |
|---|---|---|---|---|---|---|
| user-stance | 36 | 11 | 6 | 7 | 12 | **52.8%** |
| belief-correction | 121 | 42 | 28 | 27 | 24 | 42.1% |
| **project-fact（抽樣）** | **60** | **13** | **29** | **10** | **8** | **30.0%** |

**猜測的方向對，但「低到不值得跑」是錯的。** 30% 換算到剩下的 388 條約是 116 條可用記憶
（95% 信賴區間 18.4%–41.6%，即 71–161 條），而目前整個池子的可用記憶只有 88 條——
全跑會讓可用量增加一倍以上。

另一個訊號：project-fact 的 PARTIAL 佔 48%，遠高於整體的 29%。
陳述仍偏複合，或者模型對這類「知道一半」。這是下一輪蒸餾指示可以再收緊的地方。

**艾斯維爾裁決：跳過全跑，直接進 B/C。** 附帶更正一個先前的說法——
這 388 條**之後仍然測得準**：校準用的是乾淨受測 agent 加上已經固定的 statement，
掛 hook 不會改變任何一個。被污染的是**未來新蒸餾出來的 concept 分布**
（agent 事先知道了某個坑就不會再踩，belief-correction 這類會自然變少）。

## B：PreToolUse 注入 hook（已實作，尚未掛載）

`hook_pretooluse.py`。走 PreToolUse 而不是 UserPromptSubmit，因為**只有檔案訊號量過
precision**；另一個理由是 query 品質——human 輪次的 `user_text` 中位數只有 58 字元，
大量是「繼續」「可以」。

三道閘門：只注入 `surprisal >= 0.8` 的條目、`overlap >= 2`、同一 session 同一條只注入一次。

### 🚨 A1 的門檻差點被錯誤地搬過來

`MIN_FILE_OVERLAP = 2` 在 A1 那邊是「記憶的錨點 ∩ 那一輪編輯的**檔案與符號**」，
初版 hook 卻拿它去比對**單次呼叫的單一檔案**——那樣 overlap 最高就是 1，
等於永遠不注入。兩個 overlap 名字一樣，不是同一個量。

真實語料上的觸發率（671 個有編輯的輪次）：

| 比對內容 | 池子 | overlap≥1 | overlap≥2 |
|---|---|---|---|
| 只有檔案 | 通過的 88 條 | 3.1% | **0.0%** |
| 只有檔案 | 全池 605 條 | 22.2% | 5.2% |
| 檔案 + 符號 | 通過的 88 條 | 70.8% | **39.9%** |
| 檔案 + 符號 | 全池 605 條 | 94.0% | 74.8% |

原因是 **252/343 條有檔案錨點的記憶只有一個檔案錨點**，兩個檔案重疊湊不出來。
符號補上這一項，門檻才有意義。

符號拿得到是因為 `tool_input` 就是完整的工具參數——Edit 的 `old_string`/`new_string`、
Write 的 `content` 都在裡面。並且要**跨同一輪的多次呼叫累積**（換 `prompt_id` 就重置），
才對得上 A1 的「一輪碰過什麼」。

定案：**39.9% 的編輯輪次會注入，平均 1.52 條**。

### 🚨 掛上去才發現：檔案錨點在 hook 裡本來完全失效

`tool_input.file_path` 是**絕對路徑**，而 `anchors` 是蒸餾者寫的 **repo 相對路徑**。
`file_key` 取末 3 段，於是：

```
絕對路徑 → testseperatememorysystem/agent_memory_spike/retrieve.py
錨點     →                          agent_memory_spike/retrieve.py
```

兩邊永遠不相等，**檔案錨點一項都不會命中，只剩符號在起作用**。
修法是先 `normalize_path(target, repo_root(cwd))` 再取比對鍵。

**上面那組觸發率數字（overlap>=1 為 70.8%、>=2 為 39.9%）是修正後才成立的**——
量測時比對的兩邊都是語料裡的相對路徑，所以量測看不到這個落差，
而修正前 hook 的實際觸發率比它低。

與「`overlap>=2` 搬進 hook 就歸零」是同一型的錯：**同名的量不一定是同一個量**，
而且兩次都是靜默的——不會報錯，只是少召回。
`test_inject.py` 原本 10 項測試全部直接餵 `select()` 的集合，繞過了這一段，
所以補了一項走完整 `run()` 的測試。

### 查證過的 hook 行為

- `PreToolUse` 支援 `hookSpecificOutput.additionalContext`，以 system-reminder 形式
  插在**工具結果旁邊**，模型看得到
- payload 有 `prompt_id`（所以 `injections.jsonl` 的鍵從 prompt 指紋改成 `prompt_id`，
  比指紋精確——指紋要求兩邊對 `user_text` 的組法完全一致，而那沒有保證）
- payload **沒有** prompt 文字
- 預設 timeout 600 秒（`UserPromptSubmit` 才是 30 秒）

## C：自動化管線（已實作，尚未排程）

`pipeline.py`。把「收料 → 健檢 → 蒸餾 → 收斂 → 校準」串成一條可排程的管線，
裁決走 headless `claude -p`——與手動派 subagent 等價（同一個模型、同一組工具、
同一份訂閱額度），不額外花錢。

```bash
python pipeline.py --status                    # 看上次跑到哪
python pipeline.py --run --dry-run             # 印出要做什麼
python pipeline.py --run --max-groups 40       # 實跑，成本封頂
python pipeline.py --run --stage calibrate     # 只跑一個階段
```

三道安全閥：**lockfile**（疊跑會重複寫入，因為 watermark 是跑完才寫的）、
**每次上限**、**前一階段失敗就停**（語料壞掉時蒸餾只會蒸出錯的記憶）。

### headless 不是「subagent 的另一個名字」——四個實測差異

一路撞出來的，每一個都會讓整條管線靜默失效：

1. **它會吃全域 `CLAUDE.md` 與 SessionStart 注入**，於是進入對話模式——
   前兩次實跑它先去查專案記憶、然後回問「要我從哪一項開工」，一個指令都沒執行。
   要用 `--append-system-prompt` 明確聲明非互動才會照做
2. **寫不進 `~/.claude/` 底下**（算敏感路徑，要互動批准，而 headless 沒有互動）。
   所以裁決者**只回 JSON、不寫檔**，由管線負責落地——這反而更好，
   寫入範圍被程式限死，裁決者也就不需要超過讀取的權限
3. **指令必須用相對路徑的直譯器**（`../U.E.P-s-Core/env/Scripts/python.exe`）。
   換成 `sys.executable` 的絕對路徑，裁決者的 Bash 一律被擋下——
   allowlist 認的是字面，不是解析後的路徑
4. **prompt 要走 stdin**。接在 `--append-system-prompt` 後面當位置參數時，
   實測它收不到任務內容（回「Could you send the task content?」）；
   而且 Windows 命令列有 32K 上限，判卷材料很容易撞到

### 抽不出 JSON 就算失敗，不寫空檔案

裁決者的回覆用 ```json 柵欄抽取，失敗時**不落地**。
寫一個空檔案的話，下游的 `--ingest` 會收到「零筆結果」而看起來像正常跑完——
那正是這個專案反覆踩到的靜默失敗。

退路的括號順序也有講究：回覆是陣列時先找 `{` 會抽到陣列裡的**第一個物件**，
`json.loads` 照樣成功，於是靜默地只收到一筆。要先試開始得早、涵蓋得長的那個。

### 端到端實測

`--stage calibrate --max-groups 3`：107 秒跑完，含兩次裁決（受測 + 判卷）與收回，
已校準 217 → 220。11 個管線機制測試（鎖、JSON 抽取、階段編排）。

**排程尚未掛上。** 掛的話是 Windows 工作排程器每日凌晨，
指令為 `pipeline.py --run --max-groups N`——不掛 `SessionEnd` hook：
蒸餾要跑幾分鐘，hook 得 detach 才不會卡住結束流程，detach 之後失敗是靜默的、難追，
而且剛結束工作時機器最忙。

---

# Phase 3.5 — 矛盾雙評審（已完成）

Phase 2.8 留下的兩件待辦一起收掉：**矛盾改用雙評審**、**只針對矛盾降門檻**。
兩者是同一件事的兩面——降門檻會多撈出配對，而多撈出來的東西值不值得刪，
取決於判定夠不夠可靠。

## 做法

`consolidate.py` 新增三樣東西，Phase 2.8 那次是手動拼的，沒有留在程式裡：

| 旗標 | 做什麼 |
|---|---|
| `--mode contradiction` | 只問矛盾的判定準則（見下） |
| `--run-judges N` | 用 headless `claude -p` 跑 N 位**獨立**評審，分批、可續跑 |
| `--panel <dirA> <dirB>` | 合議 → 共識（可直接 `--ingest`）+ 爭議組（可直接 `--show` 給仲裁者） |

合議規則：兩票一致就結案；**relation 相同但 `keep` 不同也算爭議**
（刪哪一條是不可逆的）；三票時走多數決，**三票全異一律不刪**——
那代表沒有任何兩個人看到同一件事。

## 矛盾專用準則與通用準則的差別是刻意的

通用準則寫「不確定就給 DISTINCT」。對重複那是對的（多留一條只浪費 context），
套到矛盾上就成了系統性低估。所以這份準則：

1. **只問一件事**：這兩條能不能同時為真——可判定的形式，而不是「關係特殊嗎」
2. **不給任何一邊當安全答案**，並要求每一組都寫理由（含 DISTINCT），
   雙評審才有分歧可比對
3. 明確排除版本演進：「舊版用 A」與「現在用 B」兩條都標了時期就不算矛盾，
   **沒標時期、寫得像現行狀態的那種才算**

## 結果：284 組、13 組真矛盾

門檻降到 0.72，扣掉歷史上真的判過的 59 組，剩 284 組。
兩位評審 × 8 批 = 16 次裁決全數成功。

```
共識 281、爭議 3 → 評審間一致率 98.9%
共識中要處理的 11 組（全是 CONTRADICTION，DUPLICATE 0）
仲裁 3 組 → 2 組矛盾、1 組推翻積極那票
合計 13 組 → 移除 12 條（c-102 被兩組各推翻一次）
池子 605 → 593
```

一致率 98.9% 遠高於 Phase 2.8 的 81%，但**兩者不可比**：這輪的判定空間只有兩個選項，
而且刻意不問重複。這個數字不能拿去宣稱「判定變可靠了」。

## 🚨 surprisal 通過不代表這條記憶還成立

被刪的 12 條裡，`c-739` 的 surprisal 是 **0.8——它已經在可用池裡**：

> 「掃描 worker 對單一法規的 fetch 沒有逾時保護，卡住時**需要手動停止再重新觸發**」

後來查明「停止」是合作式旗標，對卡在無逾時操作裡的 worker 完全無效。
這條記憶注入之後會讓人去按一個沒有用的按鈕。

**校準與收斂量的是兩根獨立的軸**：校準問「模型知不知道」，收斂問「這條還成不成立」。
一條過期的記憶反而**更容易通過校準**——模型當然不知道一個已經不成立的事實。
所以 `calibrate.py` 的通過數從來就不是可用記憶的品質保證，
**收斂必須跑在校準之前，而且模型升級後重跑校準時也要重跑收斂**。

而 `c-739` 是兩位評審一票 DISTINCT、一票 CONTRADICTION，**靠仲裁才撈出來的**。
單評審有一半的機率讓它留在可用池裡——這是雙評審這筆投資最直接的回報證據。

## 池子縮 2% 之後檢索小幅變好

```
                收斂前 605 條      收斂後 593 條
case               513                502
recall@1         13.8%              13.9%
recall@5         33.1%              33.9%
recall@10        42.3%              44.6%
MRR              0.235              0.237
```

方向與 Phase 2.5（775 → 612）一致：移除的確實是雜訊。
但**分母也變了**（被刪的條目本身是評測 case 的來源），這不是嚴格的同批對照，
2.3pp 的 recall@10 差距要打折看。

## 這輪沒有做的事

- **0.72–0.80 區間的重複沒有處理**。這條通道只問矛盾，
  措辭不同但意思相同的一律判 DISTINCT。抽樣顯示該區間重複的命中率約 3%，
  而重複只浪費 context——要處理的話得再跑一輪 `--mode relation`，值不值得未評估
- **門檻仍是 0.72**。更低的區間完全沒看過，藏了多少真矛盾未知
- **仲裁只有一票**。兩位評審分歧時由第三方定案，沒有再驗證仲裁本身的一致性


# Phase 3.6 — scope 的三態（已完成）

## 一個 `or` 讓通用知識三個月流不出去

`distill.py` 的收料端是 `concept.get("scope") or task.get("repo")`。
蒸餾指示要求「跨專案通用則填 null」，蒸餾者照做了——`None` 是假值，
`or` 直接換成觀察到它的那個 repo。**每一條通用知識都被貼上單一 repo 的標籤，
沒有錯誤、沒有紀錄。**

先前記的「蒸餾端沒產出 global 這個分類」是錯的：它有產出，被收料端吃掉了。
池子裡 global 的數量是精確的零——不是稀少，是被歸零。

```
蒸餾原始輸出            780 條
scope 表態為通用         47 條 (6.0%)
  belief-correction 34 / project-fact 11 / user-stance 2
                    ↑ 價值最高的那一類佔 72%
仍存活                   35 條 → 已回填
  已校準 26、已通過門檻 6
```

## 修法是三態，不是把 `or` 換成 `is None`

| 蒸餾者寫的 | 語意 | 結果 |
|---|---|---|
| `"scope": null` | 明確表態「跨專案通用」 | `None` |
| `"scope": "Eternity"` | 明確表態屬於某 repo | `"Eternity"` |
| 沒有這個鍵 / 空字串 | 沒說 | 退回 `task["repo"]` |

**「沒說」要退回 repo 而不是 global**：標窄了只是召不到，
標成 global 會把單一專案的事實散播到所有專案。方向弄反的代價不對稱。

字串形式的 `"null"` / `"global"` / `"*"` 一併正規化成 `None`。
`scope=None` 是正典表示法，`GLOBAL_SCOPES` 只是容忍蒸餾者寫成字面值。

既有資料靠 `distill.py --backfill-scope <distill_out> [--apply]` 回填——
修收料端不會追溯已經收進來的條目。

## 🚨 分岔的判斷會讓驗證步驟說謊

回填之後注入其實**已經生效**，但 `hook_session_start --stats` 照舊印
「池子裡沒有 global scope 的記憶」：它只比對 `GLOBAL_SCOPES` 字串，
不認 `None`，而同一個檔案裡的 `select` 認。

這比原本那個 bug 更危險——它會讓人以為修復沒生效而去改已經對的程式碼。
`is_global` 現在只有一份（在 `hook_pretooluse`，另外兩條路匯入），
測試同時鎖住三條路的放行行為與 stats／select 的一致性。

同型的第二個誤導：stats 的可注入數用 `min(該 scope 條數, TOP_K)` 分開算，
但一個 repo 拿得到的是「專屬 + 全部 global」合併後取 top-k。
本 repo 專屬只剩 1 條時它印「實際注入 1 條」，實際候選是 7 條。**改用 select 實算。**

## 合併吃掉了 4 條，回填救不回

47 條表態通用的裡面有 12 條已被 `consolidate` 判 DUPLICATE/CONTRADICTION 移除。
其中 8 條的贏家同樣是通用表態（回填一併救到），**4 條的贏家是專案特有的陳述**：

| 輸家 | 贏家 | 贏家的樣子 |
|---|---|---|
| c-303 | c-417 | 「Eternity 的 `.ssp-body` 不能用 CSS 多欄」 |
| c-541 | c-229 | 「Eternity 的 `scripts/perf-measure.mjs` 在 headless 下…」 |
| c-383 | c-069 | 「…只有 `origin.kind == 'human'` 才是真人輸入」 |
| c-052 | c-588 | Noto Serif TC 子集化此前從未實作過（CONTRADICTION） |

前兩條明顯是通用知識被合併進專案特有的措辭裡，**收斂把它們窄化了**。
這是 DUPLICATE 判定的一個未處理的失效模式：兩條講同一件事、
但一條是通用陳述一條綁了專案，選 keep 的準則寫「留寫得更具體的那條」，
於是**系統性地偏好窄的那一邊**。要修得在準則裡加上 scope 的考量，本輪沒做。

## 找去向時踩到自己記過的坑

追查那 12 條時，我把六個 `consolidate_pairs*.json` 的配對塞進同一個 dict——
各檔都從 `p-0000` 起編，後載入的直接蓋掉前面的，於是 12 條全部顯示「找不到裁決」。
README 前面就寫過「同鍵多來源的索引要決定性處理」。
**配對表與裁決必須按批次成組處理。**

# Phase 3.7 — 三條路重測（已完成）

scope 修好、收斂跑完之後重測。**每條路都切開 global 與 repo 條目分別看**，
否則總數會把兩種完全不同的行為平均掉。

| 觸發點 | 判定數 | 嚴格 precision | 上一輪 | global 條目的 precision |
|---|---|---|---|---|
| PreToolUse（top-3, overlap≥2） | 101 | **58.4%** | 43.5% | — （只有 1 條達到門檻） |
| PreToolUse（top-5, overlap≥1） | 242 | 36.8% | 30.5% | 21.4%（n=14） |
| UserPromptSubmit | 63 | **12.7%** | 28.1% | **0.0%**（n=21） |
| SessionStart | 145 | **5.5%** | 6.5% | 2.2%（n=91） |

PreToolUse 情境命中率 66%（上一輪 62%）。

## 🚨 通用知識在弱訊號路徑上是負價值

這是反直覺的部分：解鎖 global 之後，**兩條弱訊號路徑都變差或持平**。

- **UserPromptSubmit 28.1% → 12.7%**。global 佔了 33% 的召回，
  RELEVANT **0 條**。扣掉 global 之後 repo 條目是 19.0%——仍低於上一輪，
  因為 global 進了 BM25 的候選池，會擠掉本來會被選中的 repo 條目。
- **SessionStart 6.5% → 5.5%**。global 佔 63% 的注入額度而 precision 只有 2.2%。
  原因是這條路排序只看 surprisal，而 global 對**每個** repo 都是候選——
  同樣那幾條高 surprisal 的通用記憶會在所有 session 反覆佔滿 top-5。

**「跨專案通用」講的是這條記憶在哪裡成立，不是它在哪裡切題。**
沒有相關性訊號的路徑無法分辨這兩件事，於是 global 條目變成穩定的雜訊源。
scope 過濾對它們按定義不起作用——這正是它們最需要過濾的時候。

## PreToolUse 變好，但不是 global 的功勞

58.4% 比上一輪高 15pp，而**達到定案門檻的 global 條目只有 1 條**。
改善來自池子本身（605 → 591，兩輪收斂移除的是雜訊），不是這次的 scope 修復。

反過來說也成立：**唯一掛載的那條路幾乎用不到通用知識**。
global 條目的錨點通常是概念名（`columns`、`rm -rf`、`Timing-Allow-Origin`）
而不是檔案路徑，湊不到 `overlap>=2`。scope 修復讓資料正確了、
讓通用知識在**有訊號的路徑**上可用了，但線上收益目前接近零。

## 量測介面補齊

`retrieve.py --judge-precision` 補上，三條路現在同一個介面
（`--dump-precision` / `--judge` / `--ingest`）、同一個判卷後端。
先前唯一掛載的這條反而是唯一只能人工判卷的。

# Phase 4 — PreToolUse 全域掛載（2026-08-10）

**掛載的只有兩條**：`hook_stop.py`（收料）與 `hook_pretooluse.py`（注入），
都在 `~/.claude/settings.json`。`hook_session_start.py` 與 `hook_userpromptsubmit.py`
留著當對照組——它們量出「沒有相關性訊號會有多差」（6.5% / 12.7%），
那正是 58.4% 的參照點——**但不掛**。

## 掛之前補的：hook 觀察對帳

見 `transcript.py` 的 `TOUCH_LOG`。觸發量放大好幾倍之前，
要先讓「hook 漏看編輯」這型遺漏看得見——它不會報錯。

## 決策時的數字

**延遲**（每次編輯都付，timeout 10 秒）

```
本 repo      中位數  90 ms   p90  98
Eternity     中位數  97 ms   p90 100
AI-Website   中位數  88 ms   p90  94
```

跨 repo 一致。**首次冷啟動約 700 ms**（檔案系統快取未熱），仍差兩個數量級。

**觸發率極度不均**（語料 697 個編輯輪次模擬）

```
AI-Website-API             237 輪 → 73.0%     TestSeperateMemorySystem  31 輪 →  6.5%
AI-Website                 129 輪 → 40.3%     AI-Website-Web            92 輪 →  1.1%
Eternity                   195 輪 → 20.0%     JSAI-Main-Website / Functions →  0.0%
────────────────────────────────────────────────────────────────
合計                       697 輪 → 38.3%     平均注入 1.3~1.6 條
```

**掛全域實際只有兩個 repo 會有感**，原因單純是池子分布
（85 條通過門檻裡 AI-Website-API 佔 31、Eternity 22、AI-Website 17）。

⚠️ **38.3% 是上界，不是預期值**：模擬用的語料**正是這些記憶的來源**，
真實新工作的觸發率必然更低。掛上去之後的實測才是真數字。

**成本**：statement 中位數 107 字元，每次注入約 441 字元。
語料 37 天／210 session，推估每天約 7 次注入、3.2k 字元。

## 操作上唯一的坑：不要兩邊都掛

專案的 `.claude/settings.local.json` 與全域 `~/.claude/settings.json`
同時掛的話，本 repo 每次編輯會跑兩次——`already` 節流讓第二次跳過已注入的，
然後**挑別的條目補上**，於是一輪注入 6 條而不是 3 條，touch 紀錄也會重複。
移到全域時專案端那段必須一併移除。
