# 串流管線整合專案設計（Echo Stream）

> 狀態：草案，待艾斯維爾裁決
> 日期：2026-08-20
> 寫在 TTS 專案的 docs 下只是暫時的，定案後整份移到新 repo。

---

## 1. 問題診斷：上次 Discord 為什麼慢

2026-06-22 的實測記錄了兩個數字：**LLM 回覆延遲 30-40 秒**、**STT 管線因 event loop 被阻塞 30 秒而停擺**。

後者已修（`_handle_decision` 改 fire-and-forget），但前者是結構性的。把當時的鏈路攤開：

```
使用者說完話
  └─ STT 等整段音訊轉錄完          ~1-2s
      └─ Memory retrieve（bge-m3）  ~1-3s
          └─ LLM 等整段生成完        ~20-30s
              └─ TTS 等整段合成完    ~5-15s
                  └─ 開始播放
```

**每一層都等前一層 100% 完成，四段耗時直接相加。**

這就是為什麼「先做 TTS 串流」不能解決問題——TTS 只是最後 5-15 秒，前面 25 秒照樣要等。

### 關鍵指標搞錯了

真正決定體感的不是**總處理時間**，是 **TTFA（Time To First Audio）**——使用者說完話到聽見第一個字的間隔。人類對話的自然停頓約 200-500ms，超過 2 秒就明顯尷尬，超過 5 秒對話就斷了。

總時長可以長（回答本來就可能很長），但 TTFA 必須短。

---

## 2. 核心設計：句子級 pipeline

不是「每一層都做成串流」，而是**在句子邊界讓各層重疊**。

```
t=0.0  使用者說完
t=0.3  STT 吐出最終轉錄
t=0.5  LLM 開始串流 token
t=1.2  ├─ 累積到第一個句子邊界「我覺得這個想法不錯。」
t=1.2  │   └─ 立刻送 TTS 開始合成 ─────┐
t=2.0  ├─ LLM 還在生成第二句           │
t=2.4  │                          第一句音訊就緒 → 開始播放  ← TTFA 2.4s
t=2.8  ├─ 第二句邊界 → 送 TTS         │
t=3.5  │                          第二句合成中（第一句還在播）
t=4.2  └─ LLM 結束                    │
t=5.0                            第二句接上播放，無縫
```

LLM 生成第二句的時間，被 TTS 合成第一句的時間吃掉了。**兩層重疊，不是相加。**

TTS 本身的段級串流（我們已經做好的 `on_segment_audio`）是這個架構的最後一哩，但**句子級切分才是主要的延遲來源解法**。

### 三個必要條件

| 條件 | 現況 | 缺口 |
|------|------|------|
| LLM 支援 token 串流 | Gemini / Ollama 都支援 | Bot 現在用非串流呼叫 |
| 句子邊界即時偵測 | 無 | 要寫（中日英標點 + 長度上限保險） |
| TTS 接受連續投餵 | `on_segment_audio` 已有 | 需要 sentence queue 包一層 |

---

## 3. 為什麼要獨立專案

**支持開，但要限制範圍。**

理由：

1. **Discord bot 有太多與管線無關的複雜度** — DAVE 加密、presence 自動加入、cogs 權限、Stage Channel 變通。在那裡除錯管線，永遠分不清是管線問題還是 Discord 問題。
2. **管線需要能單獨量測** — 每一段的延遲要能被打點、比較、回歸測試。塞在 bot 裡做不到。
3. **五個子系統各自獨立 repo，缺一個中立的整合層** — 現在 bot 同時扮演「整合層」和「Discord 前端」，兩個角色綁死。
4. **可以用 fake 模組先跑通骨架** — 這是你說的「先確保整個對應沒有問題」的關鍵，見 §6。

**風險：變成第六個孤島。** 防範方式：這個專案**不實作任何推理邏輯**，只做三件事——定義契約、串接、量測。所有實際能力都來自 adapter 包裝的既有 repo。

### 命名

組織慣例是 `TestSeparate*System`，但這個專案的性質相反（整合而非分離）。建議：

- `EchoStream` — 簡潔，與 echo_stt / echo_tts 命名一致
- `TestIntegratedPipeline` — 沿用 Test 前綴表明實驗性質

我傾向 `EchoStream`，因為它終將成為正式的整合層，不是拋棄式實驗。

---

## 4. 架構

```
┌─────────────────────────────────────────────────────────┐
│  Frontend Adapter（可插拔）                              │
│  CLI / Web(現有 tts_server) / Discord / 檔案批次          │
└────────────┬────────────────────────────▲───────────────┘
             │ AudioChunk / Text          │ AudioChunk
             ▼                            │
┌─────────────────────────────────────────┴───────────────┐
│                    Pipeline Core                         │
│                                                          │
│   InputStage ──► ThinkStage ──► SpeakStage               │
│   (STT)          (LLM+Mem)      (TTS)                    │
│      │               │              │                    │
│      └───────────────┴──────────────┘                    │
│              LatencyTracer（全程打點）                     │
└──────────────────────────────────────────────────────────┘
             │              │              │
             ▼              ▼              ▼
      echo_stt        Memory/Session    echo_tts
      (worker)        (in-process)      (worker)
```

沿用先前定案的 **Option D 混合模式**：GPU 推理（STT/TTS）走獨立 worker process，CPU 模組（Memory/Session）in-process。這個決策不變，只是搬到新專案。

### 三個 Stage 的契約

```python
class InputStage(Protocol):
    """音訊或文字 → 使用者的一個 turn"""
    async def stream(self, source: AudioSource) -> AsyncIterator[Utterance]: ...

class ThinkStage(Protocol):
    """turn → 逐句吐出回應"""
    async def stream(self, utterance: Utterance) -> AsyncIterator[Sentence]: ...

class SpeakStage(Protocol):
    """句子 → 逐段吐出音訊"""
    async def stream(self, sentences: AsyncIterator[Sentence]) -> AsyncIterator[AudioChunk]: ...
```

三個都是 `AsyncIterator`，這是整個設計的重點——**任何一層都不回傳「完整結果」**，只回傳流。

### SentenceSplitter（新元件，關鍵）

介於 ThinkStage 和 SpeakStage 之間：

```python
async def split(tokens: AsyncIterator[str]) -> AsyncIterator[Sentence]:
    """把 LLM 的 token 流切成可合成的句子。

    切點優先序：
      1. 句末標點（。！？.!?）+ 後續非標點
      2. 次級標點（，、；,;）且累積長度 > min_chars
      3. 長度上限強制切（避免長句拖垮 TTFA）

    第一句用較低的 min_chars（快速出聲），後續句子放寬（品質優先）。
    """
```

這裡有個張力：**切太細，語調破碎；切太粗，TTFA 拉長。** 首句用 8-15 字就切，後續 25-40 字，是我建議的起點——實際值要用聽感測，就像我們調情緒強度那樣。

---

## 5. 延遲預算

目標 **TTFA < 2.5s**（含 STT）。分配：

| 階段 | 預算 | 現況 | 備註 |
|------|------|------|------|
| STT 最終轉錄 | 400ms | ~1-2s | large-v3-turbo int8 已是甜蜜點；要靠 VAD 提早切 |
| Memory retrieve | 300ms | 1-3s | **可平行化**：與 LLM prefill 同時跑，或首句跳過 |
| LLM 首句 | 800ms | 20-30s（整段） | 串流後首 token ~200-400ms，累積到句子 ~800ms |
| TTS 首段 | 1000ms | 4-5s（整段） | 已實測：段級串流首段 4.0s，需靠短首句降到 1s |
| **TTFA 合計** | **2.5s** | **~30s** | |

**最大的兩個槓桿**：LLM 改串流（省 ~20s）、首句切短（省 ~3s）。Memory 平行化是第三。

`LatencyTracer` 對每個 turn 記錄這五個數字，寫成 JSONL。沒有量測就沒有優化——上次就是因為只有「30-40 秒」這個總數，不知道該修哪裡。

---

## 6. 分階段計畫

### Phase 0：骨架 + Fake 模組（半天）

**這是「確保對應沒問題」的關鍵。** 三個 Stage 都用 fake 實作：

- `FakeInputStage` — 從 stdin 或預錄文字讀 turn
- `FakeThinkStage` — 用固定回應，模擬 token 串流（帶可設定的延遲）
- `FakeSpeakStage` — 產生 sine wave，模擬合成耗時

跑通後就能驗證：契約對不對、背壓會不會爆、取消（使用者插話）能不能正確中斷、LatencyTracer 打點準不準。**全程不需要 GPU，秒級迭代。**

驗收：`python -m echo_stream demo --fake` 能完整跑一輪對話，輸出延遲報告。

### Phase 1：接真 TTS（1 天）

只換 SpeakStage，其餘維持 fake。TTS adapter 包 `IndexTTS2Backend`，用現有的 `on_segment_audio`。

驗收：TTFA < 1.5s（fake LLM 首句立即給），音訊無縫。

### Phase 2：接真 LLM + SentenceSplitter（1-2 天）

換 ThinkStage。這裡要實作 SentenceSplitter 並調參。

驗收：文字輸入 → 語音輸出，TTFA < 2s，長回應的句間銜接自然。

### Phase 3：接真 STT（1-2 天）

換 InputStage，包 `MultiChannelSTTEngine`。

驗收：麥克風輸入 → 語音輸出全鏈路，TTFA < 2.5s。

### Phase 4：Memory / Session 接入（1-2 天）

ThinkStage 內部接 `MemoryEngine` + `ContextManager`。重點是**不能讓 retrieve 阻塞首句**。

### Phase 5：Discord Frontend（另計）

到這裡管線已經驗證過，Discord 只是換一個 Frontend Adapter。DAVE 等問題與管線無關，可獨立處理。

---

## 7. 設計決策

### 已定案（艾斯維爾 2026-08-20）

**7.1 專案名稱：`Echo Stream`** — 定位是「測試串流的專案」，與 echo_stt / echo_tts 命名一致。

**7.2 支援插話（barge-in）** — 播放中使用者開口要能中止，且中止訊號往上游傳播（TTS 停播 → LLM 停止生成 → 丟棄剩餘 token）。契約因此多一級複雜度：每個 Stage 都要能 `cancel()`，並保證取消後不留下半寫入的狀態。

這條決定了 Phase 0 的骨架必須從一開始就有取消路徑——事後補會很痛。

**7.3 Memory + Session Control 都要接** — 不是只接 LLM。`ThinkStage` 內部組成：

```
Utterance → SessionControl(ContextManager 壓縮) → LLM 串流
                    ▲                              │
                    └── EchoMemory(retrieve/store) ┘
```

### 待討論

**7.4 Memory retrieve 的時機**（我的問題沒問清楚，下次要收斂）

已確定「要接」，但**在管線的哪個時間點 retrieve** 還沒定：

- (a) LLM 之前（阻塞首句，現況做法，成本 1-3s）
- (b) 與 LLM prefill 平行，首句不等它 ← 我的建議
- (c) 只在 Intent 判定需要時才 retrieve

(b) 的理由：多數回應的第一句是承接語（「嗯，我想想」「這個問題」），不需要長期記憶。但 (b) 有個副作用——首句可能與後續記憶內容不一致（例如首句說「我不記得了」，第二句記憶回來了）。需要決定能否接受。

**7.5 一輪對話的邊界誰決定** — 明確待議。

先前為 Discord 多人場景設計的決策中間件（Turn Aggregator → Address Gate → Intent Triage → Session Router，見 [PM] U.E.P's Mind Reflourished 2026-06-22）在單人測試場景過重。傾向 Phase 0-4 用 VAD 靜音判定的簡單版，Phase 5 再接完整版，但契約要留位置。

與 7.2 的插話有交互作用：**插話事件本身就是一種 turn 邊界訊號**，兩者要一起設計。

**7.6 音訊格式統一** — STT 吃 16kHz，TTS 吐 22.05kHz，Discord 要 48kHz stereo。建議重採樣放 Frontend Adapter，Pipeline 內部用各模組原生取樣率。

---

## 9. 待研究：外部工程參考

艾斯維爾點名想參考的方向，下個 session 開始調查：

**CosyVoice 3 的 text-in streaming + audio-out streaming** — 雙向串流架構。CosyVoice 2 的紀錄是 first-audio 150ms（我們先前調查過），3 代的雙向串流設計值得看它怎麼處理「文字還在進來時就開始出音訊」——這正是我們 SentenceSplitter 想解決的問題，可能有更好的切法（例如不切句子，而是用 chunk-aware causal flow matching 直接支援不完整輸入）。

其他值得一併看的：

- **Fish-Speech S2 的 serving 架構** — SGLang continuous batching / RadixAttention prefix caching（命中率 86.4%），多輪對話的 prompt 前綴重用
- **Orpheus / FlashTTS 的 first-audio 優化** — 130-325ms，看它們的 chunk 策略
- **LLM 端的 speculative decoding** — 先前調查過 SSD/VADUSA 對 TTS 的應用，但同樣技術用在 LLM 上對縮短首句更直接

原則：這些都是**參考**，不是替換。我們的 IndexTTS 2.5 + 情感向量系統是既有資產，要找的是可以移植的**管線技術**，不是換模型。

---

## 8. 這個專案不做什麼

明確劃線，避免膨脹：

- ❌ 不實作 STT / LLM / TTS / Memory 的任何推理邏輯
- ❌ 不做 Discord 的功能（權限、指令、presence）
- ❌ 不做人設、prompt 工程（那是 Session Control 的事）
- ❌ 不做 UI（現有的 web shell 夠用了）
- ✅ 只做：契約、串接、背壓、取消、量測
