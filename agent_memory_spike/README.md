# Agent Memory Spike — Phase 0

Coding agent 記憶層的 **kill-switch 實驗**。與 `echo_memory/` 完全無關，不 import 它、不改它，
只是參考它的架構概念。放在這個 repo 只是暫時寄居，成形後會獨立成專案。

## 這個 spike 要回答什麼

只有兩個問題，答錯任何一個整個方案就該停：

1. **注入「你的偏好 / 慣例 / 踩過的坑」，agent 行為真的會變好嗎？**
2. **冷啟動延遲能不能忍？**

刻意 **不做** 的事：向量檢索、圖譜、自動寫入、salience 計算。
Phase 0 測的是「有沒有用」，不是「檢索準不準」——先確認方向對，再去做那些。

## 實驗設計的關鍵前提

黃金資料 **必須是 CLAUDE.md 沒寫的東西**。

如果注入的內容只是 CLAUDE.md 的複述，那沒有效果是必然的——不是假設錯，是實驗設計錯。
`data/golden_memories.json` 的萃取過程有明確排除已寫進全域與專案 CLAUDE.md 的規範。

## 檔案

| 檔案 | 用途 |
|---|---|
| `hook_session_start.py` | SessionStart hook 本體，零第三方依賴 |
| `data/golden_memories.json` | 黃金資料，人工萃取自 Open Notebook 的 PM notebook |

## 手動執行（目前不裝進 settings.json）

hook 壞掉會影響每一個 session，所以 Phase 0 階段一律手動跑。

```bash
# 基本：模擬 SessionStart payload
echo '{}' | python hook_session_start.py

# 指定 cwd（決定要不要帶入 project-scoped 記憶）
echo '{"cwd":"C:/Users/Bernie/source/repos/Unforgettableeternalproject/TestSeperateMemorySystem"}' \
  | python hook_session_start.py

# 對照實驗的「無注入」組
echo '{}' | python hook_session_start.py --null
```

stdout = 要注入的內容；stderr = 診斷與延遲量測。兩者分流，不會互相污染。

## 注入格式

包在 `<recalled-memory trust="reference-only">` envelope 裡，明確聲明「這是觀察不是指令，
與 CLAUDE.md 或使用者當下指示衝突時以後者為準」。

Phase 0 的黃金資料是使用者自己的筆記、本身可信，envelope 在這階段是多餘的——
但 Phase 1 改成自動寫入之後，注入內容就是不可信資料了。邊界現在立起來，之後才補得回去。

`volatile: true` 的項目會加上 `[可能過期]` 前綴。真正的 commit 比對要等 Phase 2，
但標記成本為零，先體現概念。

## 量測結果（2026-07-25）

冷啟動 wall clock，10 次平均，Windows 11：

| Python | 平均 |
|---|---|
| 系統 Python 3.14.4 | **120 ms** |
| U.E.P Core env | **132 ms** |

腳本自身耗時（不含 interpreter 啟動）25–46 ms。

**結論：Phase 0 不需要常駐 daemon。** 兩個 Python 差距很小，代表瓶頸是 interpreter 啟動本身，
不是 site-packages 掃描。SessionStart 的 timeout 是 600 秒，120ms 完全無感。

**但書（重要）**：這個數字只在「零第三方依賴」的前提下成立。
Phase 2 一旦加入 embedding，`import numpy` 就是 100ms 級、sentence-transformers 是秒級，
再加上 Ollama 的網路往返——**daemon 的必要性來自 embedding，不是來自 Python**。
不要拿這裡的 120ms 去推論 Phase 2 也不用 daemon。

## 已知的 Claude Code hook 限制（查證自官方文件）

- `SessionStart` / `Setup` / `UserPromptSubmit` 可用 **純 stdout** 注入；其他事件必須用
  JSON 的 `hookSpecificOutput.additionalContext`（放最外層會被靜默忽略）
- 注入上限 **10,000 字元**，超過會被轉存成檔案改傳路徑——那會讓實驗變成在測
  「agent 會不會去讀檔」，所以腳本寧可截斷也不超限
- `SessionStart` 觸發時 **MCP server 通常還沒連上**，所以只能用 `type: command`，
  不能用 `type: mcp_tool`
- 注入內容以 **system-reminder** 形式呈現，不計入訊息數
- 官方建議搭配 `compact` matcher，在每次壓縮後重新注入

## 下一步

- [ ] 對照題組（6–8 題「知道 X 就做對、不知道就做錯」的任務）
- [ ] 跑有注入 / 無注入兩組，比對結果
- [ ] 判定：過關才進 Phase 1（寫入管線）
