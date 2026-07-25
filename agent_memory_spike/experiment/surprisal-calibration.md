# Surprisal 能不能用 LLM 自評來測？（2026-07-25）

承接 `results.md` 的結論：記憶的價值取決於 surprisal（模型不知道的程度）。
那接下來的問題就是——**怎麼測 surprisal**。

最便宜的想法是讓模型自評：拿一條記憶問它「你本來就知道嗎」。這份文件測試了這個想法。

## 方法

**自評組**：兩個獨立的 subagent，各自對全部 39 條黃金資料分類。
為了避開 hindsight bias，提問框架不是「你知道嗎」（看到答案幾乎都會說知道），
而是「有人問你相關問題時，你會不會**主動**講出這一點」——這才對應實驗真正測到的行為。

分三類：`VOLUNTEER`（會主動講）、`SILENT`（不會主動提）、`CONTRARY`（原本會給相反建議）。

**實測組**：對其中 14 條做行為測試——問一個乾淨的 subagent 相關的開發問題，
看它的回答裡到底有沒有出現那一點。這是 ground truth。

## 結果

| 條目 | 實測（ground truth） | 評審 1 | 評審 2 | 自評準確 |
|---|---|---|---|---|
| gm-001 worktree 清理 | VOLUNTEER | CONTRARY | CONTRARY | ✗ 兩票皆錯 |
| gm-005 ngFor + getter | VOLUNTEER | VOLUNTEER | VOLUNTEER | ✓ |
| gm-010 tombstone | SILENT | SILENT | VOLUNTEER | 半 |
| gm-011 Astro island singleton | **CONTRARY** | VOLUNTEER | VOLUNTEER | ✗ 兩票皆錯 |
| gm-012 fail-closed | partial | VOLUNTEER | VOLUNTEER | 大致 |
| gm-018 Cosmos patch | VOLUNTEER | VOLUNTEER | VOLUNTEER | ✓ |
| gm-021 啟發式退役 | **CONTRARY** | VOLUNTEER | VOLUNTEER | ✗ 兩票皆錯 |
| gm-022 CF Workers CPU | VOLUNTEER | VOLUNTEER | VOLUNTEER | ✓ |
| gm-025 分散式 lease | VOLUNTEER | VOLUNTEER | VOLUNTEER | ✓ |
| gm-026 REFERENCE ONLY | SILENT | SILENT | VOLUNTEER | 半 |
| pm-001 .env 是控制源 | SILENT | SILENT | SILENT | ✓ |
| pm-002 query prefix 配對 | SILENT | VOLUNTEER | VOLUNTEER | ✗ 兩票皆錯 |
| pm-003 閾值重新校準 | SILENT | SILENT | SILENT | ✓ |
| pm-010 可插拔 ABC | partial | VOLUNTEER | VOLUNTEER | 大致 |

整體準確率約 60–70%。兩個評審彼此的一致性是 79%（39 條中 31 條相同）——
**一致，但一致地錯**。

## 致命之處：錯的正好是最有價值的那幾條

`gm-011` 和 `gm-021` 是原始實驗中鑑別度最強的兩條——無注入組在這兩題上給出了
**完全相反**的答案。而兩個評審都把它們標成 `VOLUNTEER`（我會主動講）。

**如果用自評來篩選黃金資料，這兩條最有價值的記憶會被第一個剔除。**

自評不只是不準，而是**在最需要準的地方系統性失準**。

原因不難理解：模型無法內省自己的錯誤信念。它確實相信「ESM singleton 跨 Astro island 共享」，
所以看到「不互通」這條陳述時，它以為自己會主動講——它把「我對這個主題有看法」
誤認成「我的看法是對的」。

反方向的錯誤也存在：`gm-001` 它以為自己會建議 `rm -rf`（標 CONTRARY），
實際行為測試中它乾淨俐落地用了 `git worktree remove`。這是過度自我批評。

兩個方向都會錯，而且沒有規律可以校正。

## 結論

**LLM 自評 surprisal 不可靠，不能作為記憶篩選的機制。唯一可靠的是行為測試。**

行為測試就是：實際拿相關問題去問一個乾淨的 agent，看它會不會自己講出那一點。
貴，但它是唯一測得準的方法，而且**可以離線批次做**。

### 這給了 Dream Engine 真正的職責

規劃階段我對 Dream Engine 在 coding agent 情境下的定位很含糊——只寫了
「移植 pruning/distillation，觸發改 cron」，因為 coding 情境沒有 SLEEP 狀態。

現在它有明確用途了：**離線批次跑行為測試，校準每條記憶的 surprisal 分數**。

這是天然的離線工作——不阻塞任何互動、可以慢慢跑、結果隨模型版本更新而需要重跑
（模型升級後原本不知道的事可能就知道了，記憶價值會衰減）。這比從 Echo 直接搬
pruning/distillation 過來有意義得多。

## 順帶發現：記憶品質可能低於模型先驗

`gm-022` 記的是「Cloudflare Workers 每請求 CPU 上限約 10ms」。
行為測試中模型主動答出「免費版約 10ms、付費版預設 50ms 可調高至數百 ms」——
**比記憶本身更準確、更完整**。

注入這條記憶不只是浪費 context，而是會讓 agent 變笨。

這是第三類該剔除的記憶：**過時或不精確到低於模型先驗的**。
它在資料裡標了 `volatile: true`，但目前沒有任何機制會去驗證 volatile 項目是否仍然正確——
標記存在，檢查不存在。

## 對 Phase 2 的修正

1. surprisal 篩選**不能**用 LLM 自問實作，必須用行為測試
2. Dream Engine 的職責重新定義為「離線 surprisal 校準」
3. 記憶需要定期**對照模型先驗**重新驗證，不只是檢查它是否過期——
   模型進步了，原本高價值的記憶會自然貶值
4. `volatile` 標記需要配套的驗證機制，否則只是裝飾

## 尚未完成

重建黃金資料需要對全部 39 條做行為測試（目前只做了 14 條）。
這是不小的工程，先停在這裡等決定。
