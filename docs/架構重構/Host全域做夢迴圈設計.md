# Host 全域做夢迴圈設計

對應任務卡：「P3-M Core Host 全域記憶與做夢迴圈」的後續（`P3接入方案.md` §3.1 S2 註記留下的「完整迴圈另案」），並為 P7 Proactive、P8 常駐、之後的 Schedule 設計預留介面。
性質：設計文件，不含實作。作者：奈也／奈留（ARCHITECT）。日期：2026-10-31。基準：
- Core：`feature/complete_revamp`（commit 61d33f5、c9fccc6、55a6b43）。`uep/memory/host.py` 的 `MemoryHost` 已是 `enable_dream_scheduler=False` 預設、每 engine 一個 `EchoMemoryAdapter`、`_on_dream_report` 收 listener 並計數；目前**沒有任何東西會觸發 dream**，本文設計的就是這個觸發者。
- 記憶：TestSeperateMemorySystem develop cb36629（唯讀參考）：`engine.dream(triggered_by)`、`dream_readiness()`、`configure_dream(**overrides)`、`set_dream_listener(fn)`、`FileLease`、`close(timeout)`。
- 已定裁決：房內 #129（記憶與做夢迴圈由 Core Host 全域管理，bot 的 dream 排程只是過渡）、#252／D75 附帶（休息時段即做夢時段、夜貓子情境，Schedule 之後談）、D76（bot 退場分階段 X／Y，觀察期 7 天）、D86（0.5 蒸餾門檻與 replay 0.4 門檻不動，0.4–0.5 積壓等 Host 全域迴圈處理）、Dream 宿主契約（房內 #48：多行程以檔案租約互斥、共享場域 `scope-g*` 不啟動排程）、D90（S6 在 Windows GPU 機同行程過渡，最終 Core 常駐 Mac mini）。
- 本文取代的舊註記：`P3接入方案.md` §3.1 S2 註記「做夢只由記憶端內建排程驅動（D75 (c)），Host 不提供手動 dream 入口」，以及 D75 (c)「Host 仍只 `start_dream_scheduler()`」。`host.py` docstring 已記錄 Bernie 的裁決取代 D75 (c)（per-user engine 與 `scope-g*` 關閉自己的排程，全域迴圈由 Host 管）；本文把「Host 管」具體化。`activity.py` 的「只能事後補記」也隨之改為「Host 自己觸發，可寫 ACTIVE 再 ENDED」（§5.3）。
- DECISIONS.md 的 D 編號本文新候選從 **D92** 起（D91 為現有最後一號）。VC 驗收案例前綴用 `VC-DL-`（Dream Loop），與 `VC-PD-`、`VC-RX-` 的具名系列慣例一致，不撞 `VC-2-*`。

---

## 0. 結論（先看這段）

1. **迴圈放在 `MemoryHost` 裡，是 Host 擁有的一條專屬 daemon 執行緒（`uep-mem-dream`），全域只有一條。** 它不經 `MemoryHost.call()`、不用 per-namespace executor、不用 `_AdapterRunner`：`call()` 全程持 engine 鎖，而一次 dream 動輒數分鐘；塞進去會擋住該 namespace 的前景 store／retrieve。記憶端自己的註解寫明「dream 不在此持鎖，各階段短臨界區取 storage_lock、LLM 在鎖外」，內建排程本來就是另一條執行緒直接呼叫 `dream_now`，與前景並行。這條執行緒同時天然把所有 namespace 序列化，LLM 併發固定為 1。
2. **決策與執行分離。** 「現在要不要做夢、對誰做」是一個純函式 `DreamPlanner.decide(snapshot) -> DreamDecision`（無執行緒、可用假時鐘全測）；執行緒只負責取 snapshot、呼叫 planner、執行 `engine.dream(triggered_by=...)`、處理結果。Schedule 之後只替換/擴充 planner 的輸入（休息時段、夜貓子），不動執行緒。
3. **四種觸發，皆由 Host 判斷，不使用記憶端的 `idle`／`daydream`／`cron` 字串。** `host_daydream`（待處理 ≥20）、`host_idle`（待處理 ≥5 且全域閒置）、`host_backlog`（D86：0.4–0.5 積壓，獨立觸發）、`host_rest`（休息時段）。不用 `"cron"`：它會啟動 cron 窗口去重並寫 `last_cron_completed_at`。
4. **D86 的積壓要先精確定義，否則會寫錯。** 0.4–0.5 的 episode 永遠進不了蒸餾（門檻 0.5），所以**永遠不會因蒸餾而 `is_dreamed`**；它們被 replay 處理後只會取得 `dream_scanned_at`，再依「最舊先」輪替。因此「消化」＝`dream_scanned_at` 被設定，積壓指標＝`salience ≥ 0.4 且 not is_dreamed 且 dream_scanned_at is None` 的數量。現行 `dream_readiness()` 的 `pending` 只算 ≥0.5，**看不到這個數字**，這是記憶線缺口第一條（M-D1）。
5. **前景互斥分階段。** 現階段（Phase A）只做得到「不開始」：跨所有平台的全域閒置判定加開始前再查一次。進行中的 dream 無法被 Host 中止（`dream()` 只收 `triggered_by`，沒有 deadline／cancel；`_LeaseLost` 只在續租失敗時觸發），所以做夢中來話＝讓它跑完，代價是 Model Gateway 與 GPU 競爭，不是鎖。真正的「讓位」需要記憶線的 `should_abort`（M-D2，Phase B）。用注入 `llm_fn` 拋例外來中止是權宜之計，且有嚴重副作用（中止的週期仍會發 `memory.dream_completed`），**不建議上線**（§3.4）。
6. **bot 過渡期不需要租約協商。** 階段 X 已要求 Host 與 bot 的資料目錄不相交，`FileLease` 落在各 `storage_path` 之下，兩邊連租約都不會碰到同一個檔。Host 看到 `lease_held` 的實際意涵是「同一資料目錄有第二個行程」＝P-15 違反或人工維運，應計數、退避、連續發生才升 health，而不是當正常流量。「不雙重蒸餾」的保證來自 D76 的目錄不相交與 `is_dreamed` 持久標記，不來自租約協商（§4）。
7. **事件面極簡。** 不重發 `memory.dream_completed`（那是記憶端 emitter 在週期成功結束時發、經 sink 轉譯，M6）。Host listener 對 completed 只做 metrics 與 Activity；skipped／error 走 metrics 與 log，持續失敗才發 `core.component_health_changed`（subject `memory.dream`）。是否新增 `memory.dream_cycle` kind 列為 D 候選，建議不加（§5.2）。
8. **關閉順序在既有序列前面插一步。** 新序：停止迴圈（不接新 tick）→ 有界等待進行中的 `dream()` 返回 → `stop_dream_scheduler`（現為空操作，保留）→ 排空事件投遞 → 等 executor 排空 → `adapter.aclose()` → `engine.close(timeout)`。已實作的 stop→drain→aclose→close 不動（§6.3）。
9. **五個切片，DL-0 為純函式核心，DL-4（讓位與預算強制）被記憶線 M-D2 擋住。** 需艾斯維爾裁決 9 項（D92～D100），每項附建議值（§10）；記憶線缺口 8 項，其中 M-D1（積壓計數）擋住 `host_backlog`，M-D2 擋住 Phase B（§9）。

---

## 1. 位置與執行緒模型

### 1.1 系統概覽

```
 Core Host（單程序，uep/）
 ┌──────────────────────────────────────────────────────────────────────────────┐
 │ Event Gateway → Journal → Bus ──訂閱 input.*／turn.*／conversation.*──┐      │
 │ State Coordinator（Interaction／Behavior／Health 軸）◄──current()─┐   │      │
 │                                                                   │   ▼      │
 │ ┌─────────────────────── MemoryHost（uep/memory/）──────────────────────┐    │
 │ │  engine registry：ns -> engine（鎖＋單工作緒 executor＋adapter）      │    │
 │ │   ▲ 前景：call()／adapter_call()（持 engine 鎖，短）                  │    │
 │ │   │                                                                   │    │
 │ │  ┌──────────── DreamLoop（uep-mem-dream 執行緒，全域一條）─────────┐  │    │
 │ │  │ tick ─► ForegroundProbe.snapshot() ─┐                           │  │    │
 │ │  │         readiness（每 ns，短鎖）────┼─► DreamPlanner.decide()   │  │    │
 │ │  │         RestWindowPolicy.window_at()┘          │ DreamDecision  │  │    │
 │ │  │                                                ▼                │  │    │
 │ │  │   engine.dream("host_*")  ◄── 不持 engine 鎖、不經 executor    │  │    │
 │ │  │        │ listener（同執行緒同步）→ MemoryHost._on_dream_report  │  │    │
 │ │  │        ▼                                                        │  │    │
 │ │  │   結果分類 → 退避／health／WindowOutcome                        │  │    │
 │ │  └─────────────────────────────────────────────────────────────────┘  │    │
 │ └───────────────────────────────────────────────────────────────────────┘    │
 │ 並存的其他執行緒：Core 維護迴圈（5s，presence.expire_due＋bridge.reconcile）、 │
 │ ES loop 執行緒（uep-echo-loop）、adapter loop 執行緒（uep-mem-adapter-loop）  │
 └──────────────────────────────────────────────────────────────────────────────┘
```

### 1.2 為什麼是專屬執行緒，不借用既有的

| 候選 | 結論 | 理由 |
|---|---|---|
| `MemoryHost.call()` ＋ per-ns executor | 否 | `call()` 在 fn 全程持 `_locks[ns]`；dream 數分鐘，前景 store／retrieve 全被擋。 |
| `_AdapterRunner` event loop | 否 | 那是 adapter 協程用的；阻塞呼叫會凍住所有 namespace 的寫入。 |
| ES loop 執行緒（`EsLoop`） | 否 | 「loop 上不得做阻塞 DB」；dream 是長阻塞。 |
| Core 維護迴圈（`PresenceMaintenance`，5s tick） | 否，但沿用其 tick 形狀 | 一次 tick 會阻塞到 dream 結束，會拖住 presence 到期處理。只借「定期 tick＋Event 停止」的形狀，另開執行緒。 |
| 記憶端內建 APScheduler | 否 | 已被 `configure_dream` 關閉；它只看單一 engine 的 ≥0.5 累積，看不到全域閒置與積壓，這正是 D86 卡住的原因。 |
| **`uep-mem-dream` 專屬 daemon 執行緒** | **採用** | 與記憶端內建排程相同的併行形狀（另一條執行緒直接呼叫 `dream_now`），但決策權在 Host。序列化所有 namespace。 |

**奈也觀點：** 一條執行緒、一次一個 namespace，最容易講清楚，也最不會讓 GPU 與 API 額度被做夢吃光。
**奈留觀點：** 序列化的代價是某個 namespace 的 dream 卡住（LLM 逾時、租約被占）會讓其他 namespace 都等。這要靠 §3.3 的單次時間上限（Phase B 才能強制）與 §6 的退避緩解；Phase A 期間必須在 `status()` 暴露「目前 dream 已跑多久」，讓人看得到。

### 1.3 與其他迴圈的關係

- **與 adapter loop／ES loop：** 無共享執行緒。dream 執行緒不呼叫 adapter；唯一與前景共用的資源是**同一個 engine 實例**（store／retrieve 在前景，dream 在另一條執行緒）。這個併行是否受支援，需要記憶線明示（M-D3；內建排程已這樣跑，應該是）。
- **與 Core 維護迴圈：** 互不依賴。dream 迴圈不得呼叫 presence／bridge；它對 Core 的唯一讀取是 ForegroundProbe 的兩個來源（State Coordinator 與 Bus 訂閱）。
- **與 Core 排程器：** 目前 Core 沒有通用排程器（只有上述維護迴圈）。休息時段與夜貓子屬 Schedule 設計，本文只留 `RestWindowPolicy` 介面（§2.4）；Schedule 上線後由它實作該介面，dream 迴圈不改。
- **執行期參數：** 所有門檻由 Host 端 `DreamLoopConfig` 持有，**不**靠 `configure_dream`。原因：`configure_dream` 必須在 DreamEngine 建立之前呼叫，之後固化（太晚呼叫回 `{"applied": False}`）；而 `dream_readiness()` 會建立 DreamEngine。所以迴圈**不得在 `MemoryHost.start()` 完成之前**查 readiness，且任何執行期調參（例如夜貓子順延想調 `min_pending`）都必須在 Host 端自管，不能期待改記憶端設定。

### 1.4 模組邊界

| 模組 | 所在 | 職責 | 對外介面 |
|---|---|---|---|
| `DreamLoop` | `uep/memory/dream_loop.py`（新） | 專屬執行緒、tick、執行 dream、結果分類、退避、`status()` | `start()`／`stop(timeout)`／`status()`／`pause(reason)`／`resume(reason)` |
| `DreamPlanner` | 同上 | 純函式決策：snapshot＋config＋歷史 → `DreamDecision` | `decide(snapshot) -> DreamDecision`（無 IO、無時鐘，時間由 snapshot 帶入） |
| `DreamLoopConfig` | 同上 | 全部門檻與預算（frozen dataclass） | 欄位見 §10 |
| `ForegroundProbe`（Port） | 介面在 `dream_loop.py`；實作在 `uep/memory/foreground.py`（新） | 跨平台整體前景活動快照 | `snapshot(now) -> ForegroundSnapshot` |
| `RestWindowPolicy`（Port） | 介面同上；預設 `FixedWindowPolicy` | 休息時段與夜貓子預留介面 | `window_at(now)`、`on_window_end(window, outcome)` |
| `MemoryHost` | 既有 | 擁有 `DreamLoop`；提供 target 清單、listener、metrics、Activity | 新增 `dream_status()`（診斷用） |
| `DreamActivityRecorder` | 既有 | 擴充：`record_started`／`record_finished` | 見 §5.3 |

依賴方向：`DreamLoop` -> `MemoryHost` 提供的 target 清單與 `MemoryEngineLike` Protocol（已含 `dream`、`dream_readiness`）；`DreamLoop` -> Port（`ForegroundProbe`、`RestWindowPolicy`、Clock）。**不反向**：Planner 不 import Host；Probe 的實作才 import State Coordinator 與 Bus（在 `uep/memory/foreground.py`，由組裝層注入，MemoryHost 本體不 import `uep.state`）。這與 `memory_wiring.py`「真 engine 只由一個檔接線」的做法一致。

### 1.5 介面定義

```python
# DreamLoop 的型別邊界（草案，非實作）
@dataclass(frozen=True)
class ForegroundSnapshot:
    """跨所有平台的整體前景狀態（不是 per-namespace）。"""
    interaction_state: str            # Interaction 軸：IDLE／LISTENING／THINKING／SPEAKING／WAITING_USER
    behavior_state: str               # Behavior 軸：CHAT／WORK／MISCHIEF／SLEEP
    open_episodes: int                # conversation.episode_started 尚未 ended 的數量（含語音頻道有人）
    quiet_for_s: float                # 距最近一次前景活動的秒數（見 §2.2 的活動事件集合）
    global_stop_engaged: bool         # D36
    runtime_ready: bool               # Runtime 軸 READY（DEGRADED 仍可，STOPPING／STARTING 不可）

@dataclass(frozen=True)
class NamespaceReadiness:
    namespace_id: str
    pending: int                      # salience >= 0.5 且 not is_dreamed（現有 readiness["pending"]）
    unscanned_pending: int | None     # salience >= 0.4、not is_dreamed、dream_scanned_at is None；M-D1 前為 None
    lease_held_by_other: bool
    is_dreaming: bool                 # 他處（例如人工）正在對同一 engine dream

@dataclass(frozen=True)
class DreamDecision:
    action: Literal["run", "wait"]
    namespace_id: str | None
    trigger: str | None               # host_daydream／host_idle／host_backlog／host_rest
    reason: str                       # 固定代碼；wait 時說明為何不做（用於 metrics 與診斷）
    not_before: float | None          # wait 時建議的下次評估時間（單調時鐘）

@dataclass(frozen=True)
class RestWindow:
    start: datetime; end: datetime; tz: str

@dataclass(frozen=True)
class WindowOutcome:
    cycles_run: int; cycles_failed: int; cycles_skipped: int
    pending_at_start: int             # 窗口開始時各 ns pending 之和
    concepts_changed: int             # distillation.concepts_created + concepts_updated（取自 dream() 回傳）
    backlog_remaining: int | None     # 窗口結束時 unscanned_pending 之和

@dataclass(frozen=True)
class WindowDecision:
    extend_until: datetime | None     # 夜貓子：順延休息（None＝照時窗結束）
    wants_intake: bool                # 夜貓子：希望外部「收料」（DC 等）；Schedule 消費，迴圈本身不採取行動
    reason: str

class RestWindowPolicy(Protocol):
    def window_at(self, now: datetime) -> RestWindow | None: ...
    def on_window_end(self, window: RestWindow, outcome: WindowOutcome) -> WindowDecision: ...
```

---

## 2. 觸發條件

### 2.1 目標清單與前提

- 目標＝`MemoryHost` 的**非共享** namespace（`is_scope_namespace_id` 為 False 者）。`scope-g*` 永遠不是目標，沒有自己的 dream；共享層的投影與對帳由各 owner 的 dream（`sharing_phase`）完成。因此房內 #129 的「主機平台與 Discord 的共用記憶、各平台獨有記憶都在同一個迴圈蒸餾」落成：**一條迴圈輪流對每個 owner namespace 做夢，共享投影搭便車**，而不是迴圈去 dream 共享 namespace。
- **與舊路徑互斥：** `enable_dream_scheduler=True`（engine 內建排程）與 `DreamLoop` 啟用同時為真時，`MemoryHost` 建構拒絕（`MemoryHostError`）。`_is_dreaming` 守衛只會讓其中一方 `already_running`，擋不住計數與 Activity 混亂。
- 目標清單由 provider callable 取得（`Callable[[], Sequence[DreamTarget]]`），不在建構時固化。現況 Host 的 namespace 在 `start()` 時固定；之後 bot per-user namespace 納入（階段 Y）若要在執行期新增，只需 Host 提供動態 registry，迴圈不改（動態新增／移除 namespace 是 Host 另案，列於 §9 的 Core 側缺口）。
- 啟動寬限：`startup_grace_s`（建議 300）內不做夢，避免 Core 剛啟動、ES 暖機與 GPU 載入時與做夢競爭（D90 的 S6 同機場景尤其重要）。

### 2.2 全域閒置（跨所有平台，不是 per-namespace）

記憶端 `readiness()["idle_seconds"]` 是**該 engine** 最近一次 store／retrieve 起算，只反映單一 namespace；且 Host 已把 `dream_enable_idle` 設 False，`suggested_trigger` 只會回 `daydream` 或 None。所以 Host 必須自己判斷全域閒置，來源如下：

| 訊號 | 來源 | 作用 |
|---|---|---|
| Interaction 軸 ≠ `IDLE` | `state.current(Axis.INTERACTION)` | 任何平台有 turn 進行中即為前景，`quiet_for_s = 0` |
| 前景活動事件 | Bus 訂閱：`input.text_final`、`input.utterance_final`、`turn.started`、`turn.completed`、`turn.cancelled`、`conversation.participant_joined` | 更新「最近前景活動」時間戳（單調時鐘）。Probe 只取時間戳，不讀 payload |
| 開啟中的對話 | 訂閱 `conversation.episode_started`／`episode_ended` 維護計數 | `open_episodes > 0`（例如語音頻道有人在場但暫時沒說話）→ 不開始，即使 `quiet_for_s` 夠長 |
| Behavior 軸 `SLEEP` | `state.current(Axis.BEHAVIOR)` | 等同進入休息時段（提案 §14.3：SLEEP 是角色低活動模式，Host 事件、計時、Presence 仍運作）。現階段只作為「可略過 idle 門檻」的輸入之一，Schedule 上線後由它驅動 |
| 全域停止 | `core.global_stop_engaged`／`released`（D36） | engaged 期間不開始新 dream |
| Runtime 軸 | `state.current(Axis.RUNTIME)` | 僅 READY／DEGRADED 可開始；STARTING／STOPPING 不可 |

注意：音訊不是 UEPEvent（P3 E-9），ES 在 turn 結束前不會產生事件，所以**「使用者剛開口、尚未結束一句」這段 Probe 看不到**。最大誤差＝一句話的長度。補強是 Core 側缺口：`EchoPipelineHost` 暴露 `last_voice_activity_monotonic`（VAD 判定說話中就更新），Probe 取兩者較新者。這屬 S3 的後續，不擋 DL-1（§9 Core 側 C-1）。

**奈留觀點：** 「Interaction 軸 IDLE」不等於「沒人在」：Discord 語音頻道有人但沒人講話時 Interaction 是 IDLE。所以開啟中的 episode 數必須獨立判斷，不能只看軸。

### 2.3 觸發規則（Planner 的核心）

全域閘門（任一不通過 -> `wait`，附固定原因碼）：`not_started`／`startup_grace`／`paused`／`global_stop`／`runtime_not_ready`／`foreground_active`（Interaction≠IDLE 或 `open_episodes>0`）／`daily_cap`／`dream_in_flight`。

通過全域閘門後，對每個 eligible namespace 逐一評估；`quiet_for_s` 門檻依觸發類型而異：

| 觸發 | 條件（全部成立） | `quiet_for_s` | 說明 |
|---|---|---|---|
| `host_daydream` | `pending >= daydream_pending`（20） | `>= daydream_quiet_s`（120） | 料多，不必等久閒置，但仍須短暫安靜。對應記憶端 `daydream` 語意 |
| `host_idle` | `pending >= min_pending`（5） | `>= idle_quiet_s`（1800） | 對應記憶端 `idle`，但閒置是**全域**的 |
| `host_backlog` | `unscanned_pending >= backlog_batch`（8，即一批 replay）且 `pending < min_pending` | `>= idle_quiet_s`（1800） | D86 的獨立觸發；`pending >= min_pending` 時 `host_idle` 已會做夢（replay 也順便輪替積壓），不重複觸發。需 M-D1，無 M-D1 時此觸發停用（`backlog_enabled` 預設 False 直到 M-D1 到位） |
| `host_rest` | 目前在休息時段，且 `pending >= 1` 或 `unscanned_pending >= 1` | `>= rest_quiet_s`（300） | 休息時段是專門做夢的時間，門檻放寬；詳見 §2.4 |

per-namespace 節流（避免同一個 namespace 連續被做夢、浪費 LLM）：
- `min_gap_s`（1800）：同一 namespace 兩次 dream 開始之間的最短間隔（任何結果都算，skipped 不算）。
- `host_backlog` 另有 `backlog_min_gap_s`（10800，3 小時）與 `backlog_max_per_day`（4）：因為每個週期即使只有 replay 也會跑完整階段（含矛盾重判、程序提取等），單純為消化積壓而跑，成本不低。以現況約 91 則計：每批 8，約 12 個週期；每天 4 次，約 3 天消化完，且不影響前景。
- 全域 `daily_cycle_cap`（8，滾動 24 小時，不含休息時段內的 `host_rest`）。

**D86 的覆核（為何這樣滿足）：** D86 卡住的原因是 engine 內建排程只在 ≥0.5 待處理累積夠多時觸發。Host 迴圈用 `unscanned_pending` 補上獨立觸發，所以 0.4–0.5 的積壓不再依賴 ≥0.5 的料；而 0.5 蒸餾門檻與 replay 0.4 門檻都不動（D86 (a)）。追蹤指標是 `unscanned_pending` 隨時間下降（§5.1）。

**必須在文件中講明的限制：** 純 replay 的週期（沒有 ≥0.5 的料）不會讓 0.4–0.5 的 episode `is_dreamed`，因為統一標記只在「蒸餾有產出」時才標（`dream_now` 標記策略）。replay 只強化邊與設 `dream_scanned_at`。所以：
- D86 的「消化」＝被 replay 評估過一次（`dream_scanned_at` 已設），之後依最舊先輪替；
- replay 本身沒有 neurochem／dopamine 閘（`dream_replay_da_threshold` 只存在於 config，replay `run()` 未使用；已查 `replay.py`），但會因 `no_llm`、`llm_error`、`parse_error`、`parse_empty`、`stale`、`audit_failure` 等原因整輪 skip 而**一則積壓都沒碰**。因此 `host_backlog` 週期必須看 `dream()` 回傳的 `replay.skipped_reason`：非 None 時不算消化，退避並計 `memory.dream.backlog_replay_skipped.<reason>`，且該次不計入 `backlog_max_per_day`。
- 若艾斯維爾期待的是「這些語音語料最終被蒸餾成概念」，那需要降低蒸餾門檻（D86 選項 (b)），不在本文範圍，也不會因 Host 迴圈自動發生。D86 追蹤欄應以此措辭複查。

### 2.4 休息時段與夜貓子（預留介面，現在用簡單規則）

現在（Schedule 之前）：`FixedWindowPolicy(start="03:00", end="05:30", tz=Asia/Taipei)`（與記憶端預設 cron `0 3 * * *` 的時段一致，但這裡不用 cron 機制）。

- 窗口內：Planner 以 `host_rest` 規則依序處理有料的 namespace，直到 `rest_max_cycles`（8）或窗口剩餘時間 < `cycle_deadline_s`（不開始新週期）。
- 窗口結束：迴圈組出 `WindowOutcome`，呼叫 `RestWindowPolicy.on_window_end`。預設實作回 `WindowDecision(extend_until=None, wants_intake=False)`，即**不順延**。
- 夜貓子預留：Schedule 實作自己的 `RestWindowPolicy`，在 `on_window_end` 依 outcome 判斷「語料不足或做夢不顯著」（建議預設判準：`concepts_changed == 0 且 pending_at_start < min_pending`），回傳 `extend_until`（延後休息）與 `wants_intake=True`（要求外部在原定休息時間繼續工作或到 DC 收料）。迴圈只負責：延長窗口（受 `max_extend_s` 上限，建議 7200）、把 `wants_intake` 暴露在 `status()`；**迴圈不自己去收料或發話**，那是 Schedule／Proactive 的事。
- `WindowOutcome.concepts_changed` 取自 `engine.dream()` 的回傳 dict（`distillation.concepts_created + concepts_updated`），不需要記憶線新增欄位。

**奈也觀點：** 夜貓子是很有角色味的設計，介面預留成「輸入 outcome、輸出 decision」就夠，這樣之後 Schedule 想加更多情境不用動迴圈。
**奈留觀點：** `wants_intake` 必須是純旗標，不能讓迴圈間接產生輸出。任何「去收料」都要走 Proactive／Output Gate，否則 dream 迴圈會變成繞過 Presence 的輸出路徑。

---

## 3. 排程順序、預算與前景互斥

### 3.1 多 namespace 排序與公平

一個 tick 最多開始**一個** dream。對通過閘門的候選排序鍵（由小到大）：

1. 觸發類別 rank：`host_daydream`(0) < `host_idle`(1) < `host_backlog`(2)；休息時段內 `host_rest` 不分類別，改以第 2 條起排序。
2. `last_finished_at` 最舊者先（從未做夢者最優先）——這是輪替的來源；
3. `pending` 多者先；
4. `namespace_id` 字典序（確定性，利於測試）。

公平性保證：`min_gap_s` 使同一 namespace 不會連跑；第 2 條使每個 eligible namespace 在其他 namespace 之後必輪到。唯一的飢餓風險是「某 namespace 永遠 daydream 級、其他永遠只是 idle 級」：在 `min_gap_s` 之間 idle 級 namespace 可以插隊，因為 daydream 的 namespace 在間隔內不 eligible。另設 `max_consecutive_same_class`（建議 3）：連續 3 次都是同一類別且仍有其他類別 eligible 時，下一次強制取另一類別。

### 3.2 為什麼不做 namespace 平行

LLM 併發為 1 是刻意的：dream 是低優先背景工作，與前景共用 Model Gateway 與（S6 期間同機）GPU。平行只會更快撞前景。若之後需要，是 Model Gateway 的 priority class 問題（§3.4 與 §9 C-2），不是迴圈的問題。

### 3.3 單次與每日預算

| 預算 | 參數（建議值） | Phase A（無記憶線改動） | Phase B（M-D2 後） |
|---|---|---|---|
| 單次週期牆鐘上限 | `cycle_deadline_s`（900） | **量測不強制**：記錄實際時長與逾限次數，`status()` 暴露「目前已跑多久」 | 以 `deadline` 傳入 `dream()`，記憶端在階段邊界檢查，到期以 `skipped_reason="deadline"` 收尾 |
| 單次 LLM 呼叫數 | `max_llm_calls_per_cycle`（40） | 量測（Host 包 `llm_fn` 只計數，不拋例外） | 同上，超限視同到期 |
| 每日週期數 | `daily_cycle_cap`（8，不含休息時段） | **強制**（純 Host 端） | 同左 |
| 休息時段每窗週期數 | `rest_max_cycles`（8） | **強制** | 同左 |
| 每日 LLM 呼叫總量 | `daily_llm_call_cap`（300） | 量測，達 80% 發 log | 強制 |
| 租約 TTL | 沿用記憶端預設 3600 | `cycle_deadline_s` 必須 < TTL，否則續租失敗會自己中止 | 同左 |

單次預算的「強制」之所以必須等 M-D2：Phase A 唯一能強制的手段是讓 Host 注入的 `llm_fn` 在超限後拋例外。但各階段的 `except` 會吞掉例外並繼續，週期仍「正常」收尾並發出 `memory.dream_completed`，下游（Proactive 的 `trigger_kind=memory_*`）會被一個其實什麼都沒做的完成事件誤導。所以 Phase A 只量測、不以拋例外強制。

### 3.4 與前景對話的互斥

| 情境 | Phase A 行為 | Phase B 行為 |
|---|---|---|
| 使用者正在講話／對話進行中 | **不開始**（§2.3 全域閘門）；開始前最後一刻再查一次 Probe（tick 與實際呼叫之間可能隔數百毫秒的 readiness 查詢） | 同左 |
| dream 進行中來話 | 讓它跑完；Probe 記一筆 `foreground_during_dream` 計數與該 dream 的剩餘時間，供量測 | `should_abort` 回傳 True -> 記憶端在下一個 `_renew_lease` 點結束，以 `skipped_reason="aborted"` 收尾，**不發 `memory.dream_completed`、不標 `is_dreamed`**；Host 以 `yield_backoff_s`（600）後才重新評估，連續被讓位時指數退避（上限 `yield_backoff_max_s`＝14400） |
| 前景對話與 dream 搶 LLM | 不處理（由 Model Gateway 負責） | 同左；根本解法是 Model Gateway 為 `purpose=memory_dream` 設較低 priority class（Core 側 C-2） |

**為什麼不上線 `llm_fn` 拋例外式中止（權宜之計）：**
1. 各階段 `except` 吞例外，週期以 `completed` 收尾，並發出 `memory.dream_completed`（誤導下游）；
2. 部分階段寫入已套用、部分沒有，且「重做不復原」＝下次整批重做，浪費 LLM；
3. 與前景共用同一個 `llm_fn`，需用 thread-local 辨識 dream 執行緒才不會誤傷前景呼叫。
結論：要麼等 M-D2，要麼接受 Phase A 的「不開始＋跑完」。若艾斯維爾認為 Phase A 太久，唯一可談的折衷是把 `host_idle` 的 `idle_quiet_s` 拉長（讓 dream 更不容易撞上前景），而不是做權宜中止。

**奈也觀點：** 使用者感受上，做夢中來話其實多半沒差，因為 dream 是背景 LLM 呼叫；真正會痛的是 GPU 同機（D90 的 S6）時 TTS 變慢，那個場景我們可以用較長的 `idle_quiet_s` 與 `startup_grace_s` 先擋。
**奈留觀點：** 「多半沒差」不是保證。Phase A 必須把 `foreground_during_dream` 與時長記下來，S6 實機量測後才有依據決定 Phase B 的優先級，否則無從驗證這個假設。

---

## 4. 與 bot 過渡期 dream 迴圈共存，以及切換

### 4.1 共存的實際機制

- **目錄不相交是第一道防線（D76 階段 X）。** Host 只管 Core 自己的 namespace 與資料目錄，啟動時核對與外部占用不相交（`find_overlaps`）。`FileLease` 檔案在各 engine 的 `storage_path` 之下（`.dream-lease.json`），所以**Host 與 bot 的 dream 連租約都不會碰到同一個檔**。這代表「兩個迴圈並存要靠租約互斥」在階段 X 並不成立，實際是**資料根本不重疊**。
- **租約是第二道防線，保護「同一個資料目錄出現第二個行程」。** 這包含 P-15 違反（同一 DB 兩個 Core）、記憶線人工維運（手動 `dream`）、以及未來若有人提前把 bot 的 per-user namespace 納入而未先停 bot（D76 明文不提前納入 P3）。
- **租約不保護 retrieve／store 併發。** 這點 P3 §3.4 已寫明：`FileLease` 只擋 dream 互斥，擋不住同目錄雙 engine 的讀寫。所以**絕不能**用「有租約保護」當理由讓 Host 與 bot 對同一目錄各開 engine。本文不為共存設計任何租約協商。

### 4.2 `lease_held` 與其他跳過碼的處理

迴圈只比對固定代碼（`skipped_reason`），不解析 `skipped_detail`。

| 代碼 | 意涵（階段 X） | 處理 |
|---|---|---|
| `already_running` | 同一 engine 實例已有週期進行中（例如人工呼叫了 `dream()`） | 視為正常；該 namespace 退避 `skip_backoff_s`（300）；不計失敗 |
| `lease_held` | 同一資料目錄有第二個行程持有租約（P-15 違反或人工維運） | 退避 `lease_backoff_s`（1800）；計 `memory.dream.skipped_lease`；**連續 3 次**（`lease_degraded_after`）升 `core.component_health_changed(ref_id="memory.dream", status="degraded")` 一次（冪等），成功一次即 `ready`。若 namespace 在 `expected_lease_peers`（預設空；僅在艾斯維爾裁決提前共管某目錄時填）內，則視為正常，不升 health |
| `window_completed` | 只有 `triggered_by="cron"` 才會出現 | Host 不用 `cron`，不應出現；出現時記 warning 並當一般 skipped（代表有人用 cron 手動觸發同目錄） |
| `lease_timeout` | 租約檢查逾時（storage_lock 競爭） | 退避 `lease_backoff_s`；計 `memory.dream.skipped_lease_timeout`；連續 3 次升 health degraded |
| `lease_io_error` | 租約檔無法存取（權限、磁碟、防毒） | 退避 `io_error_backoff_s`（3600）；**第一次就**升 health degraded（這是環境問題，不會自癒）；成功一次復原 |

### 4.3 「不雙重蒸餾」的真正保證

1. 同一 namespace 在同一時刻只會有一個 dream（同 engine 實例有 `_is_dreaming` 守衛；跨行程有 FileLease）；
2. 不同時刻的兩次 dream 只處理 `is_dreamed == False` 的 episode，標記持久化於統一標記步驟；
3. 階段 X 下 Host 與 bot 的 namespace 是不同的人的資料（D76），不存在「同一批資料被兩邊蒸餾」的情形；
4. 階段 Y 後同一 namespace 只有 Host 一個 owner。

邊界情形：dream 中途崩潰（租約 TTL 內他行程接手）會「重做不復原」＝整批重跑，LLM 成本重複但資料一致。這是記憶端刻意的選擇（不做 checkpoint），本設計沿用。

### 4.4 切換步驟（bot 移除排程、改接 Host）

與 P3 §3.4 的 E1～E6 對齊，補上迴圈專屬的步驟：

| 步驟 | 動作 | 驗證 |
|---|---|---|
| 0 | DL-1 部署時 `shadow=True`：迴圈照常評估、記錄決策與 metrics（`memory.dream.shadow_would_run.<trigger>`），**不呼叫 `dream()`** | 連續 3 天（`shadow_days`）：每個 pending ≥ 5 的 namespace 至少出現一次 would_run；backlog 計數與預期相符 |
| 1 | `shadow=False`：Host 迴圈對 Core 自己的 namespace 真正執行（目錄與 bot 不相交，無需協調） | E1：啟動 log 顯示迴圈已啟動，`status()` 可見各 ns 的 `next_eligible` |
| 2 | 平台擴展（P4）時 bot 停止自建 engine（E2），其 per-user namespace 改為 Host 的 `NamespaceSpec` | 掃描 bot 端 engine 建構點為 0 |
| 3 | **順序嚴格：先關 bot 對該目錄的 engine 與其 dream 排程，再讓 Host 建立同目錄 engine。** 不得有任何時刻兩邊同時對同一目錄持有 engine | 切換演練記錄；`find_overlaps` 啟動核對通過 |
| 4 | bot 端 `_start_dream_scheduler` 與 hub 聚合迴圈以設定關閉（E3） | 設定旗標與單元測試 |
| 5 | 觀察期 7 天（D76）：bot 觸發的 dream 為 0；`memory.dream.skipped_lease` 為 0；每個 `pending >= min_pending` 的 namespace 至少被 Host 蒸餾一次 | E4 的計數器；`lease_held` 連續次數 |
| 6 | 回滾：Host 設定 `dream_loop.enabled=false`＋重啟，bot 旗標切回。**不支援不重啟的熱回滾**（Host 沒有執行期 detach namespace，§9 C-3），回滾演練（E5）須包含此重啟 | 演練一次，資料計數一致 |

---

## 5. 觀測與事件

### 5.1 指標（`MemoryMetrics` 計數器；`status()` 暴露快照與 gauge）

沿用既有：`memory.dream.runs.<trigger>`、`memory.dream.skipped_lease`、`memory.dream.skipped`、`memory.dream.error`。`<trigger>` 現在是 `host_daydream`／`host_idle`／`host_backlog`／`host_rest`（由 `_TRIGGER_SAFE` 正規化），與記憶端內建排程的 `idle`／`daydream`／`cron` 在報表上自然分開，E4 的「bot 觸發為 0」可直接用前綴判讀。

新增計數器：
- `memory.dream.skipped.<code>`：依固定代碼分項（`lease_held`、`lease_timeout`、`lease_io_error`、`already_running`、`window_completed`）；既有 `skipped_lease` 保留作彙總。
- `memory.dream.decision.wait.<reason>`：Planner 不做夢的原因分佈（`foreground_active`、`not_enough_pending`、`min_gap`、`daily_cap` 等），診斷「為什麼一直沒做夢」。
- `memory.dream.shadow_would_run.<trigger>`：shadow 模式。
- `memory.dream.foreground_during_dream`：Phase A 量測用（§3.4）。
- `memory.dream.budget.cycle_deadline_exceeded`、`memory.dream.budget.llm_calls_exceeded`：Phase A 只計數。
- `memory.dream.health.degraded`、`memory.dream.health.recovered`。

`status()` 欄位（診斷用，不含記憶內容）：迴圈狀態（running／paused／shadow）、目前 in-flight 的 namespace 與已跑秒數、每個 namespace 的 `pending`／`unscanned_pending`／`last_finished_at`／`next_eligible`／`consecutive_failures`／`backoff_until`、滾動 24 小時的週期數與 LLM 呼叫數、最近一次 `WindowOutcome` 與 `wants_intake`。

追蹤 D86 的核心指標：每個 namespace 的 `unscanned_pending` 時間序列（應單調下降）與 `host_backlog` 的觸發次數。

### 5.2 Core 事件（對齊 CC-1 §1.3 與既有 `memory.*`）

| 來源 | 行為 | 理由 |
|---|---|---|
| 週期成功結束 | **不由 Host 發事件。** 記憶端 emitter 已發 `memory.dream_completed`（M6），經 sink 轉譯器落 journal | 單一事實來源；重發會造成重複（且 Host 沒有 `evidence_refs`） |
| `skipped`／`error` | 只走 metrics 與 log，**不發 Core 事件** | 這是運維資訊，不是對話或記憶事實；避免 journal 洗版（每次 tick 的 skipped 可能很多） |
| 持續失敗／環境問題 | `core.component_health_changed`，`source="core.health"`，`payload={"ref_id": "memory.dream", "status": "degraded"\|"ready"}` | 既有 kind；State Coordinator 的 Health 軸以 `ref_id` 為 subject，新增 `memory.dream` 這個 subject 不需改註冊表或 coordinator（subject 是字串，狀態只需在 `ready／degraded／unavailable` 內）。已查 `uep/`：Health 投影只寫 Health 軸，**目前沒有任何程式把 Health subject 聚合成 Runtime DEGRADED**（Runtime 只由 `set_runtime` 寫入），所以做夢 degraded 不會連動整個 Core；日後若加聚合，須排除 `memory.dream`（D98）既有的 `memory` subject 代表整個 MemoryHost 可用性，兩者分開，讓「記憶可用但做夢壞了」可辨識 |
| 休息時段 outcome | **不發事件**，in-process 回呼 `RestWindowPolicy.on_window_end` | Schedule 與迴圈同行程；不需為此新增跨元件事件 |

**D 候選（D99）：是否新增 `memory.dream_cycle` kind**（payload 白名單：`status`、`trigger`、`skipped_reason`、`duration_ms`、`schema_version`）。**建議不加**：目前沒有消費者需要「skipped／error」事實；`memory.dream_completed` 已覆蓋成功；Schedule 走 in-process。若 P7 Proactive 之後需要訂閱失敗事實，依 D81 模式再議，需 S1 式的註冊表與 payload 白名單測試。**本文不直接寫成已定。**

### 5.3 Activity

`P3接入方案.md` §3.1 的做法是事後補記一筆已結束的 maintenance Activity（因為當時 Host 看不到 dream 何時開始）。現在 Host 自己觸發，可以：
- 開始時 `record_started(namespace_id, trigger)` -> 寫 `ACTIVE`、`kind=maintenance`、`presence_bound=False` 的 `ActivityRow`；
- 結束時（listener 內）`record_finished` 改為更新同一列為 `ENDED`＋`outcome`（`completed`／`failed`／`yielded`／`skipped_<code>`）；
- 崩潰殘留由既有 `sweep_stale()` 標 `interrupted`（已實作，啟動時呼叫）。
租約檢查發生在 `dream_now` 內部，Host 呼叫 `dream()` 前無法預知會不會 skipped，所以 ACTIVE 列一定先寫；skipped 回來時（`lease_held`／`already_running` 等）把**同一列**更新為 `ENDED`、`outcome="skipped_<code>"`，不留懸空列（否則會被 `sweep_stale` 誤標 `interrupted`）。仍是「繞過 TaskManager 直接寫 `ActivityRow`」的唯一例外，沿用。

待確認風險：ACTIVE 的 maintenance Activity 是否影響 State Coordinator 的 `action_entry_blocked`／`task_entry_blocked`。從 `uep/` 的 `ActivityKind.MAINTENANCE` 使用位置看，目前僅 `states.py` 定義與 `activity.py` 寫入，沒有閘門讀取它；DL-1 實作時以測試固定「ACTIVE maintenance 不阻擋 task／action 進入」（VC-DL-10）。

### 5.4 listener 的角色

`_on_dream_report` 是 metrics／Activity 的**單一寫入點**。Host 觸發的 dream 是在 dream 執行緒上同步呼叫 listener（早於 `dream()` 返回）。為避免雙計：`dream()` 的回傳值**只用於**取得 `WindowOutcome` 所需的概念數與時長，不再另外計數。listener 以 thread-local 的「目前 job」把報告綁回 Planner 的決策（`DreamJob(trigger, namespace_id, started_monotonic)`）。報告到達時沒有 job（人工或他處對同一 engine 觸發）：照 `triggered_by` 原字串計數，不動 Activity、不進退避與 `WindowOutcome`。

---

## 6. 失敗與恢復

### 6.1 退避

| 結果 | 退避 | 說明 |
|---|---|---|
| `completed` | 無（只受 `min_gap_s`） | 清除該 namespace 的 `consecutive_failures` |
| `error`（`status="error"`，含 `_LeaseLost` 中止） | 指數：`error_backoff_base_s`（600）× 2^(n-1)，上限 `error_backoff_max_s`（14400）；僅該 namespace，**不影響其他 namespace** | 序列化執行下，其他 namespace 在退避期間照常輪到 |
| `dream()` 拋例外（`RuntimeError`：engine 已關閉、未初始化等） | 同 `error`；若為 engine 已關閉則停止對該 target 的評估 | 迴圈本身不得因單次例外死亡：每個 tick 外層 `try/except`，只記例外類別 |
| 租約類 | 見 §4.2 | |
| 連續 `error` ≥ `error_degraded_after`（3） | 升 `memory.dream` health degraded | 成功一次復原 |

記憶端 `_trigger_dream` 的內建退避（idle 重置計時、daydream 設 `_daydream_backoff_until`）只在**它自己的排程路徑**生效；Host 直接呼叫 `dream()` 走 `dream_now`，不經那段，所以退避必須由 Host 自管（上表）。

### 6.2 迴圈自身的健全性

- 執行緒存活：`status()` 帶 `last_tick_at`；組裝層的維護或監控可據此判斷迴圈卡死。tick 外層捕捉一切例外，連續 `tick_failure_limit`（5）次例外後暫停迴圈並升 health degraded（避免狂轉）。
- `dream()` 本身卡住（Phase A 無法中止）：`status()` 的「已跑秒數」超過 `cycle_deadline_s` × 2 時 log error 並升 health degraded，供人工介入；不嘗試殺執行緒。
- 首次 `dream()`／`readiness()` 會觸發 `ensure_initialized()` 與 DreamEngine 建構（可能載入 embedding，較重）：`startup_grace_s` 之後的第一次 tick 才查 readiness，且 readiness 查詢用 `lock.acquire(timeout=0.5)` 取 engine 鎖，取不到就跳過該 namespace 這個 tick，避免與前景長操作互相卡住。

### 6.3 Host 停機順序

現有 `_stop_locked`：`stop_dream_scheduler`（每個 engine）-> 逐個 `drain` -> 各 executor 排空 -> `adapter.aclose()` -> `engine.close(timeout)`。新序：

```
MemoryHost.stop()
  0. 發 health "unavailable"（既有）
  1. DreamLoop.stop(timeout=dream_stop_wait_s)        ← 新增
       a. 設 stopping：不再接受新 tick、不再開始新 dream
       b. 若有 dream 在途：有界等待其 dream() 返回（Phase B 先設 should_abort=True）
       c. 逾時：記錄 warning（含 namespace 與已跑秒數），不阻塞後續關閉
  2. stop_dream_scheduler()（每個 engine；現為空操作，保留以防舊路徑 enable_dream_scheduler=True）
  3. 逐個 engine.event_emitter.drain(timeout)      ← dream 結束會發 memory.dream_completed，必須在 drain 之前等完
  4. 各 namespace executor 排空
  5. adapter.aclose()（排空寫入）
  6. engine.close(timeout)（內含 _wait_dream_idle 作第二道保險，冪等）
```

要點：
- 迴圈的 stop 必須在 `drain` **之前**，否則 dream 結束時才發出的 `memory.dream_completed` 會落在已排空的投遞佇列之後而遺失。
- Phase A 的 `dream_stop_wait_s` 建議 60：dream 可能還要數分鐘，Core 關機不應被它拖住太久。逾時後 dream 執行緒是 daemon，會被行程結束帶走；租約 TTL 讓下次啟動能接手，「重做不復原」使資料一致。已知風險：`engine.close` 的存檔步驟可能與仍在跑的 dream 階段併行，由 `engine.close` 的 `_wait_dream_idle(timeout)` 緩解（記憶端行為，見 M-D3）。Phase B 有 `should_abort` 後此風險基本消除。
- 組裝層（`CoreAssembly._close_inputs`）的序不變：ES Host 先停（它的 store 還要用 memory）-> MemoryHost（含迴圈）-> 入口。
- 啟動序：迴圈**最後**才 `start()`，在 `MemoryHost.start()` 內所有 namespace 完成「建 engine -> `configure_dream` -> 注入 -> `set_dream_listener`」之後（沿用「先注入再 start」契約）。啟動失敗的回收路徑（`_stop_locked` 的 `except BaseException`）要能處理「迴圈尚未建立」。

---

## 7. 切片

| 切片 | 內容 | 驗收條件 | 依賴 | 平行 |
|---|---|---|---|---|
| **DL-0 決策核心** | `DreamLoopConfig`、`DreamPlanner.decide()`、`ForegroundSnapshot`／`NamespaceReadiness`／`DreamDecision`、`RestWindowPolicy` 與 `FixedWindowPolicy`、退避計算（純函式）。無執行緒、無 IO | VC-DL-3、5（Planner 層）、6、7（退避表）、12（Planner 層）；假時鐘全覆蓋邊界值（門檻 T-1／T／T+1） | 無 | 可立即開工 |
| **DL-1 迴圈與 Host 整合** | `DreamLoop` 執行緒、`ForegroundProbe`（State＋Bus 實作，組裝層注入）、listener 綁定 job、metrics、Activity ACTIVE／ENDED、`status()`、`shadow` 旗標、停機序（§6.3）；**`host_backlog` 預設停用**（待 M-D1） | VC-DL-1、2、4、9、10、11、15、16 | DL-0；不需記憶線改動 | |
| **DL-2 積壓觸發** | 啟用 `host_backlog`：讀 `unscanned_pending`、節流（間隔與每日上限） | VC-DL-5（整合層）；以真 engine 或高擬真 fake 驗證 `unscanned_pending` 隨週期下降 | DL-1；**M-D1** | 與 DL-3 平行 |
| **DL-3 休息時段與失敗處理** | 休息窗口排程、`WindowOutcome`、`on_window_end` 回呼、延長窗口、租約／error／io_error 分類退避、health 升降級 | VC-DL-7、8、12、13 | DL-1 | 與 DL-2 平行 |
| **DL-4 讓位與預算強制（Phase B）** | `should_abort`／`deadline` 接線、前景活動 -> abort、退避、預算強制、Core 側 Model Gateway priority（若已具備） | VC-DL-14 | DL-1、DL-3；**M-D2** | |
| **DL-5 bot 切換** | shadow 觀察、namespace 接手順序、回滾演練、E1～E6 計數器對帳（bot 端與 P4 協調） | 見 §4.4 | DL-1；P4 Discord Adapter 正式化 | 晚於 P3 |

建議 PR 順序：DL-0 -> DL-1 -> {DL-2 ∥ DL-3} -> DL-4（等 M-D2）；DL-5 隨 P4。DL-1 之後 Core 即可在 `shadow=True` 下上線觀察；真啟用 Host 觸發前先過 D92～D96 的裁決。

---

## 8. 驗收案例（VC-DL-*）

驗收測試皆以 fake engine＋假時鐘＋假 Probe 驅動，不啟動真 dream；標「整合」者需真 `FileLease`（暫存目錄）或真 echo_memory。

| ID | 案例 | 預期 |
|---|---|---|
| VC-DL-1 | 啟動序：traced fake engine；另測 `enable_dream_scheduler=True` 與 `dream_loop.enabled=True` 同時為真 | 後者 Host 建構即拒絕（內建排程與 Host 迴圈會對同一 engine 各自觸發，計數與 Activity 會混）；前者順序為 建 engine -> `configure_dream(關 idle／cron)` -> 注入 -> `set_dream_listener` -> 所有 namespace 完成 -> 迴圈 `start()`；全程不呼叫 `start_dream_scheduler`；`scope-g*` 不在目標清單，永遠不被 `dream()` |
| VC-DL-2 | 兩個 namespace 同時 eligible | 任何時刻 in-flight 的 `dream()` 不超過 1（以 fake dream 計並發峰值） |
| VC-DL-3 | 前景互斥（不開始）：Interaction≠IDLE；`open_episodes>0` 且 `quiet_for_s` 很大；`quiet_for_s` = 門檻-1／門檻；global_stop；Runtime=STOPPING | 前三者與最後兩者 `wait`（對應原因碼），門檻處 `run`；`host_daydream` 與 `host_idle` 的門檻各自獨立；tick 與呼叫之間前景變活躍時，呼叫前最後一次檢查擋下 |
| VC-DL-4 | 不擋前景：fake dream 以 Event 卡住 | 卡住期間對同一 namespace 的 `MemoryHost.call()` retrieve 與 `adapter_call` store 在其 timeout 內正常返回（證明 dream 不佔 engine 鎖與 executor） |
| VC-DL-5 | D86 積壓：`pending`=0、`unscanned_pending`=91、閒置足夠 | 觸發 `host_backlog`；每次後 fake 讓 `unscanned_pending` 減 8；`backlog_min_gap_s` 與 `backlog_max_per_day` 生效；`pending>=min_pending` 時改走 `host_idle`，不同時觸發兩次；`unscanned_pending=None`（無 M-D1）時此觸發停用且不報錯 |
| VC-DL-6 | 公平：三個 namespace 皆 eligible | 連續 N 次選擇輪替涵蓋全部；同 namespace 不違反 `min_gap_s`；daydream 級常駐的 namespace 不使 idle 級 namespace 餓死（`max_consecutive_same_class`） |
| VC-DL-7 | 租約結果分類：`lease_held`／`lease_timeout`／`lease_io_error`／`already_running`／`window_completed` | 各自退避符合 §4.2；`lease_held` 連續 3 次才升 health（只發一次，冪等），成功後復原；`lease_io_error` 第一次即升；`expected_lease_peers` 內的 `lease_held` 不升；只比對 `skipped_reason`，改動 `skipped_detail` 不影響行為 |
| VC-DL-8 | 失敗隔離與退避：A namespace 連續 error，B 正常 | A 指數退避至上限；B 照常輪到；迴圈不因 `dream()` 拋 `RuntimeError` 而死亡；tick 連續例外超限暫停並升 health |
| VC-DL-9 | 停機：dream 在途時 `stop()` | 追蹤序為 迴圈 stop < `stop_dream_scheduler` < drain < executor 排空 < `aclose` < `engine.close`；逾時（`dream_stop_wait_s`）後仍繼續後續關閉且記 warning；停機後無新 dream；`stop()` 冪等；啟動失敗回收路徑在迴圈未建立時不炸 |
| VC-DL-10 | 計數與 Activity 單一寫入：Host 觸發一次成功 dream | `runs.host_idle` 只 +1；Activity 為同一列 ACTIVE -> ENDED（`completed`）；error 為 `failed`；崩潰後 `sweep_stale` 標 `interrupted`；skipped 時同一列更新為 ENDED（`skipped_<code>`），無懸空 ACTIVE；ACTIVE maintenance 不阻擋 task／action 進入 |
| VC-DL-11 | 事件面：成功／skipped／error 各一次 | Host 不發 `memory.dream_completed`；skipped／error 不產生 Core 事件；health 事件 `source="core.health"`、`ref_id="memory.dream"`；任何事件 payload 不含記憶內容 |
| VC-DL-12 | 休息時段：窗口內多個 namespace 有料 | 依 §3.1 排序逐個處理至 `rest_max_cycles`；窗口剩餘 < `cycle_deadline_s` 不開新週期；窗口結束組出 `WindowOutcome`（`concepts_changed` 取自 `dream()` 回傳）；預設 policy 不順延；自訂 policy 回傳 `extend_until` 時延長且不超過 `max_extend_s`；`wants_intake` 只出現在 `status()`，迴圈不產生任何輸出或事件 |
| VC-DL-13 | 過渡共存（整合）：兩個行程對同一暫存目錄、真 `FileLease`，同時 `dream_now` | 僅一方進入，另一方回 `lease_held`；同一批 episode 只被標記一次；不同目錄的兩個 engine 完全不互相影響（階段 X 的現實情境）；`find_overlaps` 對相交目錄拒絕啟動 |
| VC-DL-14 | 讓位與預算（Phase B，M-D2 後） | 前景活動 -> `should_abort` -> 該週期在數秒內收尾，`skipped_reason="aborted"`；**不發 `memory.dream_completed`、無 `is_dreamed` 被標記**；`yield_backoff_s` 生效且連續讓位指數退避；到達 `cycle_deadline_s` 或 `max_llm_calls_per_cycle` 同樣收尾 |
| VC-DL-15 | shadow：`shadow=True` | 記錄 would_run 決策與計數，`dream()` 呼叫數為 0；切為 False 後下個 tick 開始真執行 |
| VC-DL-16 | 暫停與寬限：`startup_grace_s` 內、`pause()` 後、global_stop engaged | 不開始；`resume()` 或解除後恢復；暫停期間 `status()` 仍可讀 |

---

## 9. 缺口與前置

### 9.1 記憶線需配合（記憶 repo，唯讀參考，由記憶線實作）

| # | 缺口 | 為何需要 | 擋住 |
|---|---|---|---|
| **M-D1** | `dream_readiness()` 補 `replay_pending`（salience ≥0.4 且 not is_dreamed）與 **`unscanned_pending`**（再加 `dream_scanned_at is None`），並回報各自的 threshold；或開放 `count_pending(min_salience, unscanned_only=…)` | 現行 `pending` 只算 ≥0.5（`KnowledgeDistillation.MIN_SALIENCE_THRESHOLD`），Host 看不到 D86 的積壓 | DL-2（`host_backlog`） |
| **M-D2** | `dream(triggered_by, *, should_abort: Callable[[], bool] \| None = None, deadline: float \| None = None)`：在每個 `_renew_lease` 點與各階段邊界檢查；中止時以 `skipped_reason="aborted"`（或 `"deadline"`）收尾，**不發 `memory.dream_completed`、不標記 `is_dreamed`**，租約正常釋放 | Phase B 的讓位與預算強制；也是停機時快速收尾的前提 | DL-4 |
| **M-D3** | 明示並以測試固定：同一 engine 實例上，`dream()` 與 `store_episode`／`retrieve`／`dream_readiness()` 跨執行緒併行受支援；`dream_readiness()` 在 dream 進行中可呼叫且不長時間占 `storage_lock`；`engine.close()` 在 dream 仍在跑時的行為（`_wait_dream_idle` 逾時後 close 與 dream 的交錯）有定義 | 本設計整個建立在「dream 與前景併行」上；停機風險（§6.3） | DL-1 的信心 |
| **M-D4** | `triggered_by` 允許值約定：自由字串，`host_*` 前綴保留給 Host，記憶端不得對它做特殊處理；並確認 `dream_now` 直呼時 `idle`／`daydream` 的內建退避不生效、cron 窗口去重只對 `"cron"` | 避免未來記憶端改動使 Host 觸發字串意外觸發別的行為 | DL-1 |
| **M-D5** | `lease_state()` 回傳補 `holder_owner`（租約檔已有，取得時傳入的 `triggered_by`） | 讓 Host 診斷時能分辨持有者是 Host（`host_*`）還是別人（人工、bot）；選配 | 診斷品質 |
| **M-D6** | `dream()` 回傳 dict 的欄位穩定性：`distillation.concepts_created／concepts_updated`、`skipped_reason`、`error` 視為契約並加測試 | `WindowOutcome` 取用 | DL-3 |
| **M-D7** | 首次 `dream()`／`dream_readiness()` 的初始化成本說明，或提供 `warmup()` | 避免在 tick 內意外卡數十秒（§6.2） | 選配 |
| **M-D8** | 選配：`dream()` 提供「僅 replay」或 `phases=` 參數（R3） | 為消化積壓而跑完整階段成本不成比例 | 成本最佳化，不擋 DL-2 |

### 9.2 Core 側前置（本專案）

| # | 項目 | 說明 |
|---|---|---|
| C-1 | `EchoPipelineHost` 暴露語音活動時間（VAD 說話中即更新） | 補 Probe 看不到「一句話尚未結束」的盲區（§2.2）；屬 S3 後續，不擋 DL-1 |
| C-2 | Model Gateway 為 `purpose=memory_dream` 設較低 priority class | 前景與 dream 搶 LLM 的根本解法；Host 自己的 `llm_fn` 要走 Gateway 與 `data_egress`（P3 §3.1 已要求） |
| C-3 | MemoryHost 動態 namespace attach／detach | 階段 Y 的 bot namespace 接手與無重啟回滾；目前 `start()` 時固定。迴圈已用 provider 設計，不阻擋 |
| C-4 | `uep/memory/foreground.py`：Probe 的 State＋Bus 實作 | DL-1 的一部分 |

---

## 10. 需要艾斯維爾裁決（D 候選，從 D92 起）

| 編號 | 問題 | 選項與建議 | 影響 | 最晚 |
|---|---|---|---|---|
| **D92** | 迴圈位置與執行模型 | (a) MemoryHost 內單一專屬執行緒、一次一個 namespace（建議）；(b) 每 namespace 一條執行緒平行；(c) 獨立行程。建議 (a)；(b) 待 Model Gateway 有 priority class 後再議，(c) 與 D70 一同在 P8 前評估 | DL-0／DL-1 | DL-1 開工前 |
| **D93** | 全域閒置定義與門檻 | `idle_quiet_s`＝**1800**（與記憶端 idle 預設 30 分鐘一致）、`daydream_quiet_s`＝**120**、`rest_quiet_s`＝**300**；`open_episodes>0` 一律不開始；`startup_grace_s`＝**300**；全域停止期間不開始。S6 同機 GPU 實測後再調 | §2 | DL-1 前 |
| **D94** | 0.4–0.5 積壓觸發與節流（D86 的落實） | `backlog_batch`＝**8**、`backlog_min_gap_s`＝**10800**、`backlog_max_per_day`＝**4**；以 `unscanned_pending` 為指標；0.5／0.4 門檻不動。**並請確認「消化」的定義＝replay 評估過一次（`dream_scanned_at`），不等於蒸餾成概念**（§2.3）；若期待後者須回 D86 選項 (b)。需 M-D1 | DL-2 | M-D1 到位前 |
| **D95** | 單次與每日預算 | `cycle_deadline_s`＝**900**、`max_llm_calls_per_cycle`＝**40**、`daily_cycle_cap`＝**8**（不含休息時段）、`rest_max_cycles`＝**8**、`daily_llm_call_cap`＝**300**。Phase A 只強制週期數類，其餘量測；強制待 M-D2 | §3.3 | DL-1 前（量測）／DL-4 前（強制） |
| **D96** | 前景讓位策略 | (A) 只做「不開始」＋跑完（Phase A，建議先上）；(B) `llm_fn` 拋例外式權宜中止（**建議不採**，會發出誤導的 `memory.dream_completed`）；(C) `should_abort` 真中止（Phase B，建議為終態，需 M-D2）。建議 A -> C，跳過 B | §3.4、DL-4 | DL-4 前 |
| **D97** | 休息時段預設與夜貓子上限 | 預設窗口 **03:00–05:30，時區 Asia/Taipei**；`max_extend_s`＝**7200**；顯著性預設判準（`concepts_changed==0 且 pending_at_start<min_pending`）只是 `FixedWindowPolicy` 的暫定值，**正式判準與夜貓子行為併入之後的 Schedule 設計**。現階段預設 policy 不順延 | §2.4 | Schedule 設計 |
| **D98** | 租約類跳過的處置與 health 升級 | `lease_held`／`lease_timeout` 連續 **3** 次升 degraded；`lease_io_error` 第 1 次即升；退避 `lease_backoff_s`＝**1800**、`io_error_backoff_s`＝**3600**、`skip_backoff_s`＝**300**；`expected_lease_peers` 預設空；**做夢 degraded 不連動 Runtime 軸**（目前無聚合，日後若加須排除 `memory.dream`） | §4.2、§6 | DL-3 前 |
| **D99** | 是否新增 `memory.dream_cycle` kind | 建議**不加**（成功已有 `memory.dream_completed`；skipped／error 走 metrics 與 health）。P7 若需訂閱失敗事實再依 D81 模式提 | §5.2 | P7 前 |
| **D100** | 過渡切換參數（補強 D76） | shadow 觀察 **3 天**後啟用；觀察期沿用 D76 的 **7 天**；回滾以設定＋重啟為準（不支援熱回滾）；bot per-user namespace 接手順序＝先停 bot engine、再建 Host engine（§4.4） | DL-5 | P4 |

其餘預設（可由實作決定，不需裁決）：`tick_interval_s`＝30、`poll_interval_s`（每 namespace 查 readiness 的最小間隔）＝60、`min_gap_s`＝1800、`error_backoff_base_s`＝600／`error_backoff_max_s`＝14400、`error_degraded_after`＝3、`lease_degraded_after`＝3、`yield_backoff_s`＝600／`yield_backoff_max_s`＝14400、`max_consecutive_same_class`＝3、`dream_stop_wait_s`＝60、`tick_failure_limit`＝5。

---

## 11. 風險與技術債

| # | 風險 | 影響 | 緩解 |
|---|---|---|---|
| R1 | Phase A 無法中止進行中的 dream | 做夢中來話可能與前景競爭 LLM／GPU；停機被拖長 | 長 `idle_quiet_s`；Phase A 量測 `foreground_during_dream`；盡快推 M-D2 |
| R2 | 純 replay 週期不會讓 0.4–0.5 `is_dreamed` | 對 D86 的期待落差（以為會變概念） | §2.3 與 D94 明講；追蹤 `unscanned_pending` 而非 `is_dreamed` |
| R3 | 每個週期即使只有 replay 也跑完整階段 | 為消化積壓付出不成比例的 LLM 成本 | `backlog_min_gap_s`／每日上限；若成本不可接受，向記憶線提「僅 replay」模式（可併入 M-D2 的參數） |
| R4 | Probe 對「一句話尚未結束」盲 | 少數情況下 dream 剛開始使用者就開口 | C-1；`open_episodes` 與 `quiet_for_s` 的保守門檻 |
| R5 | 序列化：單一 dream 卡住拖慢所有 namespace | 其他 namespace 長時間不做夢 | `status()` 暴露已跑秒數、超時升 health；Phase B 強制 deadline |
| R6 | 停機時 `engine.close` 與仍在跑的 dream 交錯 | 存檔與寫入競爭 | M-D3；Phase B 的 abort；`dream_stop_wait_s` |
| R7 | 有人把「有租約保護」當成共管同目錄的理由 | 讀寫併發損毀（WinError 5 類，P3 R6） | §4.1 明文；`find_overlaps` 啟動核對維持 |
| R8 | Host 門檻與記憶端設定雙來源 | 調參時不一致 | Host 的 `DreamLoopConfig` 為唯一來源；記憶端 `min_pending`／`daydream_pending` 僅作 readiness 回報，不參與決策；啟動時若與 Host 值不同只記 info |
| R9 | 時區與日界（休息窗口、滾動 24 小時） | DST 或系統時區變更造成窗口錯位 | 窗口以明確 tz 計算；滾動 24 小時用單調時鐘累計，不依賴日界 |

技術債登記：`DreamActivityRecorder` 仍繞過 TaskManager 直接寫 `ActivityRow`；Host 目前不能執行期增減 namespace；Model Gateway 尚無 priority class。

---

## 12. 範圍外

不設計 Schedule 本體、夜貓子情境的實際行為（工作續行、DC 收料、順延細節）與 Behavior 軸 SLEEP 的驅動；不改記憶端程式（只列缺口）；不處理 `scope-g*` 共享層自己的維護（由 owner 的 dream 搭便車）；不設計 bot 端的實際修改（只列 Host 側切換步驟與計數器）；不處理跨機器部署（P8 前須在 Mac mini 上重審租約檔路徑與時鐘，D90）；不改 0.5 蒸餾門檻與 0.4 replay 門檻（D86 (a)）。
