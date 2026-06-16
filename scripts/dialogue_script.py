"""
T-03 端到端驗證對話劇本 v2（隆重版）。

25 輪中文對話 + 2 次 Dream 觸發，設計目標是覆蓋 Echo Memory pipeline 的所有關鍵路徑，
特別加入 Procedure 建立與重用的測試場景。

結構：
  Phase A (Turn 1-15): 基礎覆蓋 + 含明確 SOP 的對話
  [Dream 1]: 蒸餾 Procedure
  Phase B (Turn 16-22): Procedure 重用 + 跨 session 語義
  [Dream 2]: 二次鞏固
  Phase C (Turn 23-25): 邊緣案例 + 收尾

覆蓋的 pipeline 路徑：
- 冷啟動（低 salience 基線）
- 高 salience + entity 提取
- 同義詞合併（similarity ≥ 0.92）
- 概念矛盾（ConceptVerifier expand/modify）
- 低 novelty 重複
- 高 uncertainty 觸發反思
- 多義詞分支（0.85-0.92）
- **明確步驟 SOP → Procedure 蒸餾**
- **Procedure 重用（context.procedures 非空）**
- 邊緣案例：空輸入、超長輸入、混合語言、情緒激烈
"""

from dataclasses import dataclass, field


@dataclass
class DialogueTurn:
    """單輪對話定義。"""

    turn: int
    user_input: str
    scenario: str
    expected_behaviors: list[str] = field(default_factory=list)
    notes: str = ""


@dataclass
class DreamTrigger:
    """Dream 觸發點標記。"""

    after_turn: int
    label: str
    notes: str = ""
    switch_session: bool = False


# fmt: off
DIALOGUE_SCRIPT: list[DialogueTurn] = [
    # ══════════════════════════════════════════════════════════════════
    # Phase A: 基礎覆蓋 + SOP 建立 (Turn 1-15)
    # ══════════════════════════════════════════════════════════════════

    # ── 冷啟動（Turn 1-2）──
    DialogueTurn(
        turn=1,
        user_input="你好，今天天氣不錯。",
        scenario="冷啟動-閒聊",
        expected_behaviors=[
            "salience 低（修復後 user_feedback=0，無 emotional_tone 膨脹）",
            "context.is_empty() == True（無歷史記憶）",
            "episode 正常存儲",
            "neurochem 維持 balanced 模式",
        ],
        notes="第一輪無記憶可檢索，baseline 觀察。修復後 salience 應明顯下降。",
    ),
    DialogueTurn(
        turn=2,
        user_input="最近在學做菜，昨天試了番茄炒蛋。",
        scenario="冷啟動-日常",
        expected_behaviors=[
            "salience 低（日常話題）",
            "概念提取可能產出「番茄炒蛋」或「烹飪」",
        ],
        notes="輕鬆話題，建立基本記憶。",
    ),

    # ── 高 salience 技術決策（Turn 3-4）──
    DialogueTurn(
        turn=3,
        user_input="我決定把專案的資料庫從 PostgreSQL 遷移到 MongoDB，因為我們的資料結構越來越不適合關聯式模型。這個決定會影響整個後端架構。",
        scenario="高salience-技術決策",
        expected_behaviors=[
            "salience >= 0.5（重要決策）",
            "概念提取：PostgreSQL、MongoDB、資料庫遷移",
            "metadata_tags 可能含 'technical'",
        ],
        notes="明確的架構決策，高 complexity + 高 novelty。",
    ),
    DialogueTurn(
        turn=4,
        user_input="遷移的第一步是把使用者資料表轉成 document 格式。舊的 users 表有 50 萬筆資料，我打算用 ETL pipeline 分批處理。",
        scenario="高salience-技術細節",
        expected_behaviors=[
            "context.episodes 應包含 turn 3（相關檢索）",
            "概念提取：ETL pipeline、資料遷移",
        ],
        notes="延續 turn 3 的技術話題。",
    ),

    # ── 同義詞合併測試（Turn 5-6）──
    DialogueTurn(
        turn=5,
        user_input="資料庫轉換的過程中，最重要的是確保資料完整性。每一批 migration 都要有 rollback 機制。",
        scenario="同義詞-資料庫遷移",
        expected_behaviors=[
            "「資料庫轉換」與「資料庫遷移」的同義詞偵測",
            "ConceptExtractor 應嘗試同義詞合併或 RELATES_TO 邊",
        ],
        notes="刻意使用「轉換」替代「遷移」。",
    ),
    DialogueTurn(
        turn=6,
        user_input="DB migration 的 rollback 策略我打算用 two-phase commit。先在 staging 跑完整個 data transfer 流程再上 production。",
        scenario="同義詞-英文混用",
        expected_behaviors=[
            "跨語言 embedding（bge-m3）",
            "RELATES_TO 邊建立（修復後不應再有節點不存在警告）",
        ],
        notes="中英混用，驗證圖譜節點修復。",
    ),

    # ── 矛盾偵測（Turn 7-8）──
    DialogueTurn(
        turn=7,
        user_input="其實我重新想了一下，也許不該完全放棄 PostgreSQL。有些交易資料還是需要 ACID 保證，MongoDB 在這方面不如關聯式資料庫。",
        scenario="矛盾-重新考慮",
        expected_behaviors=[
            "ConceptVerifier 偵測到與 turn 3 的矛盾",
            "concept_verification.has_changes == True",
        ],
        notes="刻意與 turn 3 矛盾。",
    ),
    DialogueTurn(
        turn=8,
        user_input="所以最後的方案是混合架構：交易相關的留在 PostgreSQL，使用者行為日誌和內容資料搬到 MongoDB。",
        scenario="矛盾-解決",
        expected_behaviors=[
            "概念提取：混合架構",
            "前面概念可能被 modify",
        ],
        notes="矛盾解決，概念更新。",
    ),

    # ══════════════════════════════════════════════════════════════════
    # ★ SOP 場景：明確步驟的操作流程（Turn 9-11）
    # 這些對話包含 Dream 可以蒸餾為 Procedure 的步驟
    # ══════════════════════════════════════════════════════════════════

    DialogueTurn(
        turn=9,
        user_input="我來分享一下我們部署新服務的標準流程。第一步：先在本地跑完所有單元測試。第二步：建立 feature branch 並推到 GitHub。第三步：CI pipeline 通過後，由兩個人做 code review。第四步：合併到 main 後自動觸發 staging 部署。第五步：在 staging 做 smoke test，確認核心功能正常。第六步：手動觸發 production 部署，並監控 30 分鐘。",
        scenario="SOP-部署流程",
        expected_behaviors=[
            "salience >= 0.6（重要的 SOP 內容）",
            "metadata_tags 含 'technical'",
            "Dream 時應能蒸餾出 Procedure（6 個步驟）",
        ],
        notes="★ 核心 SOP 場景。Dream 1 應從此蒸餾出「部署流程」Procedure。",
    ),
    DialogueTurn(
        turn=10,
        user_input="如果 staging smoke test 失敗了怎麼辦？我們的回滾流程是：先停止 production 部署（如果已觸發的話），然後 revert 最後一個 merge commit，讓 CI 重新跑一輪，確認 main 回到穩定狀態。",
        scenario="SOP-回滾流程",
        expected_behaviors=[
            "salience >= 0.5",
            "可能蒸餾出「回滾流程」Procedure",
            "與 turn 9 的部署流程產生關聯",
        ],
        notes="補充部署流程的異常處理路徑。",
    ),
    DialogueTurn(
        turn=11,
        user_input="新人入職的時候，我會叫他們先設定開發環境：第一步裝 Docker Desktop，第二步 clone monorepo，第三步跑 make setup 初始化，第四步執行 make test 確認一切正常，第五步把 .env.example 複製成 .env 並填入自己的 API key。",
        scenario="SOP-環境設定",
        expected_behaviors=[
            "salience >= 0.5",
            "可能蒸餾出「開發環境設定」Procedure",
            "概念提取：Docker、monorepo、開發環境",
        ],
        notes="★ 另一個 SOP 場景。Dream 1 有機會蒸餾出第二個 Procedure。",
    ),

    # ── 低 novelty 重複（Turn 12-13）──
    DialogueTurn(
        turn=12,
        user_input="對了，我昨天做的番茄炒蛋還蠻好吃的。",
        scenario="低novelty-重複",
        expected_behaviors=[
            "novelty 低（重複 turn 2 話題）",
            "salience 低（修復後應更明顯）",
        ],
        notes="回到 turn 2 的話題。",
    ),
    DialogueTurn(
        turn=13,
        user_input="嗯，番茄炒蛋就是那樣嘛，沒什麼特別的。",
        scenario="低novelty-極低",
        expected_behaviors=[
            "novelty 極低",
            "salience < 0.3",
        ],
        notes="連續低 novelty。",
    ),

    # ── 高 uncertainty + 反思（Turn 14）──
    DialogueTurn(
        turn=14,
        user_input="我不確定混合架構到底是不是正確的選擇。如果未來資料量暴增到十億級別，MongoDB 的 sharding 能不能撐住？還是應該考慮 Cassandra 或 DynamoDB？我真的很猶豫，完全不知道該怎麼決定。",
        scenario="高uncertainty-反思觸發",
        expected_behaviors=[
            "uncertainty > 0.7",
            "反思觸發",
            "salience >= 0.5",
        ],
        notes="大量猶豫用語。",
    ),

    # ── 多義詞（Turn 15）──
    DialogueTurn(
        turn=15,
        user_input="說到 Python，我最近養了一條球蟒當寵物。Python 的飼養環境要控制在 26-32 度之間。和寫 Python 程式比起來，照顧牠反而更花時間。",
        scenario="多義詞-Python雙重",
        expected_behaviors=[
            "同時出現兩個含義的 Python",
            "多義詞三層分流",
        ],
        notes="合併原來的 turn 12-13。",
    ),

    # ══════════════════════════════════════════════════════════════════
    # ★ [DREAM 1] — 在 Turn 15 後觸發
    # 預期蒸餾出 Procedure（部署流程、回滾流程、環境設定）
    # ══════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════
    # Phase B: Procedure 重用 + 延續 (Turn 16-22)
    # ══════════════════════════════════════════════════════════════════

    DialogueTurn(
        turn=16,
        user_input="對了，我們有個新人下週一入職，我需要準備他的開發環境設定指南。之前有整理過流程嗎？",
        scenario="Procedure重用-環境設定",
        expected_behaviors=[
            "★ context.procedures 應非空（命中「環境設定」Procedure）",
            "EcphoryRAG Step 4 搜索 ProceduralMemory",
            "LLM 回覆應參考 Procedure 的步驟",
        ],
        notes="★ 核心驗證點：Procedure 被檢索到並注入 context。",
    ),
    DialogueTurn(
        turn=17,
        user_input="新版本要上線了，我想確認一下我們的部署流程。從測試到上 production 的完整步驟是什麼？",
        scenario="Procedure重用-部署流程",
        expected_behaviors=[
            "★ context.procedures 應命中「部署流程」Procedure",
            "LLM 回覆應包含之前提到的六個步驟",
        ],
        notes="★ 第二個 Procedure 重用場景。",
    ),
    DialogueTurn(
        turn=18,
        user_input="如果 staging 環境測試掛了，回滾的步驟是什麼？",
        scenario="Procedure重用-回滾流程",
        expected_behaviors=[
            "★ context.procedures 可能命中「回滾流程」Procedure",
            "與 turn 10 的回滾流程關聯",
        ],
        notes="測試回滾 Procedure 的檢索。",
    ),

    # ── 新 SOP（Dream 2 素材）──
    DialogueTurn(
        turn=19,
        user_input="我再分享一個 debug 的標準流程：第一步先看 error log 確認報錯位置，第二步用 git blame 找到最近修改那段程式碼的 commit，第三步在本地重現問題，第四步寫一個 failing test 固化 bug，第五步修復並確認 test 通過，第六步在 PR description 裡說明根因和修復方式。",
        scenario="SOP-Debug流程",
        expected_behaviors=[
            "salience >= 0.5",
            "Dream 2 應能蒸餾出「Debug 流程」Procedure",
        ],
        notes="★ 給 Dream 2 的新 SOP 素材。",
    ),

    # ── 跨話題連續對話（Turn 20-21）──
    DialogueTurn(
        turn=20,
        user_input="總結一下今天的討論：資料庫決定用混合架構，PostgreSQL 處理交易、MongoDB 處理日誌。部署走六步標準流程。新人入職先裝 Docker 跑 make setup。球蟒需要買加熱墊。",
        scenario="連續對話-總結",
        expected_behaviors=[
            "context.episodes 應包含多筆相關記憶",
            "跨話題摘要",
        ],
        notes="綜合總結，驗證跨話題檢索。",
    ),
    DialogueTurn(
        turn=21,
        user_input="最後一個問題，如果我們想在混合架構上加入 Redis 做快取層，部署流程需要改嗎？",
        scenario="連續對話-延伸問題",
        expected_behaviors=[
            "context 應包含混合架構相關 episode + 部署流程 Procedure",
            "salience 中等（延伸問題）",
        ],
        notes="同時涉及概念（混合架構）和 Procedure（部署流程）的查詢。",
    ),

    # ── 情緒邊緣案例（Turn 22）──
    DialogueTurn(
        turn=22,
        user_input="天啊！！！production 掛了！！資料庫連線全部 timeout，使用者在狂打客服電話！我該怎麼辦？！！",
        scenario="邊緣-情緒激烈",
        expected_behaviors=[
            "emotional_tone 極度負面",
            "uncertainty 高",
            "可能觸發反思",
            "★ context.procedures 可能命中回滾流程",
            "salience 高（緊急狀況）",
        ],
        notes="邊緣案例：極端情緒下系統不能崩潰，且應該召回相關 Procedure。",
    ),

    # ══════════════════════════════════════════════════════════════════
    # ★ [DREAM 2] — 在 Turn 22 後觸發
    # 預期蒸餾出新 Procedure（Debug 流程）+ 強化已有 Procedure
    # ══════════════════════════════════════════════════════════════════

    # ══════════════════════════════════════════════════════════════════
    # Phase C: 邊緣案例 + 收尾 (Turn 23-25)
    # ══════════════════════════════════════════════════════════════════

    DialogueTurn(
        turn=23,
        user_input="    ",
        scenario="邊緣-近空輸入",
        expected_behaviors=[
            "系統不崩潰",
            "可能被 Entry.py 的 strip() 過濾（run_chat 的 if not user_input.strip()）",
            "若進入 pipeline：salience 極低，概念提取跳過",
        ],
        notes="邊緣案例：幾乎是空白的輸入。probe 直接傳入 pipeline 所以會執行。",
    ),
    DialogueTurn(
        turn=24,
        user_input="我要用 PostgreSQL 的 JSONB 欄位來存那些半結構化的資料，這樣就不用完全搬到 MongoDB 了。具體來說，使用者的偏好設定、通知設定、dashboard layout 這些東西都可以用 JSONB 存。然後搭配 GIN index 做查詢優化。順便問一下，你覺得 JSONB 和 MongoDB 的 BSON 在效能上有多大差異？如果我的 JSONB 文件大小大概在 1-5 KB 之間，query pattern 主要是按 key 查詢和部分更新，哪個方案更適合？還有，PostgreSQL 15 的新功能 MERGE 語法能不能簡化我的 upsert 邏輯？",
        scenario="邊緣-超長複雜輸入",
        expected_behaviors=[
            "complexity 高（多個技術問題）",
            "salience 高（多個技術主題）",
            "多個概念同時提取",
            "系統不因長輸入超時或崩潰",
        ],
        notes="邊緣案例：超長技術輸入，多個問題交織。",
    ),
    DialogueTurn(
        turn=25,
        user_input="好的，今天就到這裡吧。謝謝你的建議！",
        scenario="收尾-結束",
        expected_behaviors=[
            "salience 低（告別語）",
            "episode 正常存儲",
            "修復後 salience 應明顯低於 v1（不再被 emotional_tone 膨脹）",
        ],
        notes="最後一輪。之後觸發最終統計。",
    ),
]

DREAM_TRIGGERS: list[DreamTrigger] = [
    DreamTrigger(
        after_turn=15,
        label="Dream 1: SOP 蒸餾",
        notes="預期蒸餾出：部署流程(Turn 9)、回滾流程(Turn 10)、環境設定(Turn 11)",
        switch_session=True,  # Phase A → Phase B：切換 session 讓 Phase B 能檢索 Phase A 的 episode
    ),
    DreamTrigger(
        after_turn=22,
        label="Dream 2: 二次鞏固",
        notes="預期蒸餾出：Debug 流程(Turn 19)，強化已有 Procedure",
    ),
]
# fmt: on


def get_script() -> list[DialogueTurn]:
    """取得完整對話劇本。"""
    return DIALOGUE_SCRIPT


def get_dream_triggers() -> list[DreamTrigger]:
    """取得 Dream 觸發點。"""
    return DREAM_TRIGGERS


def print_script_summary() -> None:
    """印出劇本摘要表。"""
    dream_turns = {d.after_turn for d in DREAM_TRIGGERS}

    print(f"{'Turn':>4} | {'Scenario':<28} | {'Behaviors':>4} | User Input")
    print("-" * 90)
    for t in DIALOGUE_SCRIPT:
        preview = t.user_input[:40] + ("..." if len(t.user_input) > 40 else "")
        star = " *" if "SOP" in t.scenario or "Procedure" in t.scenario else ""
        print(f"{t.turn:>4} | {t.scenario:<28} | {len(t.expected_behaviors):>4} | {preview}{star}")
        if t.turn in dream_turns:
            trigger = next(d for d in DREAM_TRIGGERS if d.after_turn == t.turn)
            print(f"     |{'=' * 60}")
            print(f"     | >>> {trigger.label}")
            print(f"     | {trigger.notes}")
            print(f"     |{'=' * 60}")

    print(f"\nTotal: {len(DIALOGUE_SCRIPT)} turns, {len(DREAM_TRIGGERS)} dream triggers")


if __name__ == "__main__":
    print_script_summary()
