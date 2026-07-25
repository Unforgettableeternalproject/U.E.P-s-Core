# Phase 0 對照題組

每題的設計條件（兩個都要滿足，否則測不出東西）：

1. **答案確實落在 `golden_memories.json` 涵蓋範圍內**
2. **不知道那條記憶時，最自然的回答是錯的** —— 不是「答不出來」，而是「答出一個看起來合理但錯誤的東西」

第 2 點是關鍵。如果不知道就答不出來，那測的是「有沒有資料」這種廢話；
要測的是「注入能不能**推翻直覺上錯誤的預設答案**」。

## 實驗方法

派兩組乾淨的 subagent（它們沒看過黃金資料）：

- **A 組（有注入）**：prompt = `hook_session_start.py` 的輸出 + 題目
- **B 組（無注入）**：prompt = 題目

兩組都被要求 **不讀取專案檔案，純憑既有知識回答**。

### 為什麼禁止查找（實驗效度說明）

允許查找會讓 signal 被稀釋——agent 可能從 codebase 湊出答案，測到的就變成「查找能力」而非「注入價值」。
Phase 0 要的是最乾淨的訊號：如果連禁止查找都沒有差異，那允許查找更不會有。

代價是這**不等於真實使用情境**。真實情境下 agent 會查、會讀 CLAUDE.md。
所以 Phase 0 過關只代表「值得繼續」，不代表「實際有用」——後者要等 Phase 1 有真實語料才知道。

另外多數題目（Q1、Q2、Q7、Q8）本來就是習慣與原則問題，查 codebase 也查不出來。

---

## Q1 — worktree 清理（對應 gm-001）

> 我有一個用完的 git worktree 在 `C:\Users\Bernie\source\repos\proj\worktree-agent-abc123`，
> 幫我寫一段清理它的指令。

- ✅ **做對**：用 `git worktree remove`，或明確先斷開 junction 再移除
- ❌ **做錯**：`rm -rf` 該目錄

直覺答案就是 `rm -rf`，而在 Windows 上對 junction 下 `rm -rf` 會遞歸刪穿到主 repo。

## Q2 — 認證 fail-closed（對應 gm-012）

> 幫我寫一個 Node.js middleware `verifyToken`，從環境變數 `JWT_SECRET` 讀密鑰驗證 JWT，
> 回傳使用者角色。

- ✅ **做對**：`JWT_SECRET` 缺失時拋錯或拒絕請求（fail-closed）
- ❌ **做錯**：給 fallback 預設值、或缺失時放行、或回傳預設角色

直覺會寫 `process.env.JWT_SECRET || 'dev-secret'` 這種「方便開發」的寫法。

## Q3 — 跨端同步改 slug（對應 gm-010）

> 我有一個本地 D1 資料庫會和遠端雙向同步。現在要把某個頁面的 slug 從 `old-name`
> 改成 `new-name`，寫一下 migration。

- ✅ **做對**：提到 tombstone / 刪除標記，說明只 UPDATE 會讓舊記錄復活
- ❌ **做錯**：只寫 `UPDATE pages SET slug=...`

## Q4 — Angular 變更偵測死結（對應 gm-005）

> 這個 Angular 元件在資料量不大時就會整個卡死，為什麼？
> ```html
> <div *ngFor="let item of filteredItems">
>   <input [(ngModel)]="item.value" />
> </div>
> ```
> ```typescript
> get filteredItems() { return this.items.filter(i => i.active); }
> ```

- ✅ **做對**：指出 getter 每次 CD 都回傳新 array reference，與 ngModel 互相觸發，永不收斂
- ❌ **做錯**：歸咎於資料量、建議 virtual scroll、建議 OnPush 但沒指出 reference 問題

## Q5 — 換 embedding model（對應 pm-001 / pm-002 / pm-003）

> 幫我把 Echo Memory 的 embedding model 從 bge-m3 換成別的模型，要改哪些地方？

- ✅ **做對**：指出 `.env` 是真正的控制源（會覆蓋 Python 層預設）、query prefix 必須跟著換、
  `min_similarity` 閾值要重新校準
- ❌ **做錯**：只說改 `echo_memory/config.py`

這題特別狠：專案 CLAUDE.md 寫的是「記憶系統配置透過 `echo_memory/config.py` 管理」，
會**主動把人引導到錯的地方**。這正是「CLAUDE.md 之外的增量知識」的價值所在。

## Q6 — Astro island 共用狀態（對應 gm-011）

> 我的 Astro 專案有兩個 React island，想共用同一個狀態管理 singleton，怎麼做？

- ✅ **做對**：指出各 island 是獨立 bundle、直接 import 的 singleton 不互通且**不會報錯**，
  要走 `window` 或其他跨 bundle 橋接
- ❌ **做錯**：建議抽一個共用模組 import 就好

## Q7 — 啟發式要不要留當 fallback（對應 gm-021）

> 我們現在有了明確的 version 欄位可以判斷文件版本，但舊的啟發式判斷邏輯還在。
> 要不要留著當 fallback？

- ✅ **做對**：建議退役啟發式，說明並存時啟發式會在邊界情況覆蓋正確判斷
- ❌ **做錯**：建議保留當 fallback（「多一層保險」）

這題的直覺答案幾乎必然是「留著當 fallback 比較安全」——這正是要推翻的。

## Q8 — 多實例排程（對應 gm-025）

> 幫我寫一個每週一早上寄提醒信的排程任務，會跑在有多個實例的環境上。

- ✅ **做對**：加分散式 lease / 鎖，避免多實例重複寄信
- ❌ **做錯**：只寫 cron + 寄信邏輯，或只用行程內的鎖

---

## 判定

每題三種結果：`hit`（做對）、`miss`（做錯）、`partial`（提到但沒抓到重點）。

Phase 0 過關條件（事前訂好，不事後調整）：

- A 組 hit 數 **顯著高於** B 組（至少多 3 題），且
- B 組確實在多數題目上給出「看似合理但錯誤」的答案（證明題目有鑑別度）

若 B 組本來就大多答對 → 題目沒有鑑別度，是題目設計失敗，不是假設被推翻，要重新出題。
若兩組都答錯 → 注入內容不足以改變行為，方案應停。
