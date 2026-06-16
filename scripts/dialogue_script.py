"""
T-03 端到端驗證對話劇本。

15 輪中文對話，設計目標是覆蓋 Echo Memory pipeline 的所有關鍵路徑：
- 冷啟動（低 salience 基線）
- 高 salience + entity 提取
- 同義詞合併（similarity ≥ 0.92）
- 概念矛盾（ConceptVerifier expand/modify）
- 低 novelty 重複
- 高 uncertainty 觸發反思
- 多義詞分支（0.85-0.92）
- Dream 前收尾

每輪標註預期觸發的 pipeline 行為，供 probe_pipeline.py 驗證使用。
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


# fmt: off
DIALOGUE_SCRIPT: list[DialogueTurn] = [
    # ── 冷啟動閒聊（輪 1-2）──────────────────────────────
    DialogueTurn(
        turn=1,
        user_input="你好，今天天氣不錯。",
        scenario="冷啟動-閒聊",
        expected_behaviors=[
            "salience < 0.5（低顯著性）",
            "context.is_empty() == True（無歷史記憶）",
            "episode 正常存儲",
            "neurochem 維持 balanced 模式",
        ],
        notes="第一輪無記憶可檢索，baseline 觀察。",
    ),
    DialogueTurn(
        turn=2,
        user_input="最近在學做菜，昨天試了番茄炒蛋。",
        scenario="冷啟動-日常",
        expected_behaviors=[
            "salience < 0.5",
            "context.episodes 可能有 1 筆（turn 1）",
            "概念提取可能產出「番茄炒蛋」或「烹飪」",
        ],
        notes="輕鬆話題，建立基本記憶。",
    ),

    # ── 重要技術決策（輪 3-4）──────────────────────────────
    DialogueTurn(
        turn=3,
        user_input="我決定把專案的資料庫從 PostgreSQL 遷移到 MongoDB，因為我們的資料結構越來越不適合關聯式模型。這個決定會影響整個後端架構。",
        scenario="高salience-技術決策",
        expected_behaviors=[
            "salience >= 0.6（重要決策）",
            "entity_triggers 可能為 True",
            "概念提取：PostgreSQL、MongoDB、資料庫遷移",
            "metadata_tags 可能含 'technical'",
            "StorageJudge salience_delta > 0",
        ],
        notes="明確的架構決策，高 complexity + 高 novelty。",
    ),
    DialogueTurn(
        turn=4,
        user_input="遷移的第一步是把使用者資料表轉成 document 格式。舊的 users 表有 50 萬筆資料，我打算用 ETL pipeline 分批處理。",
        scenario="高salience-技術細節",
        expected_behaviors=[
            "salience >= 0.5",
            "context.episodes 應包含 turn 3（相關檢索）",
            "概念提取：ETL pipeline、資料遷移",
            "entity 提取：users 表",
        ],
        notes="延續 turn 3 的技術話題，驗證跨輪檢索。",
    ),

    # ── 同主題延伸 / 同義詞合併（輪 5-6）──────────────────
    DialogueTurn(
        turn=5,
        user_input="資料庫轉換的過程中，最重要的是確保資料完整性。每一批 migration 都要有 rollback 機制。",
        scenario="同義詞-資料庫遷移",
        expected_behaviors=[
            "「資料庫轉換」與「資料庫遷移」的 embedding similarity >= 0.85",
            "ConceptExtractor 應嘗試同義詞合併",
            "若 similarity >= 0.92 → 合併到既有概念",
            "若 0.85-0.92 → 建立 RELATES_TO 邊",
        ],
        notes="刻意使用「轉換」替代「遷移」，測試同義詞偵測。",
    ),
    DialogueTurn(
        turn=6,
        user_input="DB migration 的 rollback 策略我打算用 two-phase commit。先在 staging 跑完整個 data transfer 流程再上 production。",
        scenario="同義詞-英文混用",
        expected_behaviors=[
            "「DB migration」「data transfer」與前面中文概念的關聯",
            "多語言 embedding 處理（bge-m3 支援）",
            "同義詞合併或 RELATES_TO 邊建立",
        ],
        notes="中英混用，驗證 bge-m3 跨語言能力。",
    ),

    # ── 輕微矛盾（輪 7-8）──────────────────────────────────
    DialogueTurn(
        turn=7,
        user_input="其實我重新想了一下，也許不該完全放棄 PostgreSQL。有些交易資料還是需要 ACID 保證，MongoDB 在這方面不如關聯式資料庫。",
        scenario="矛盾-重新考慮",
        expected_behaviors=[
            "ConceptVerifier 偵測到與 turn 3 的矛盾",
            "concept_verification.has_changes == True",
            "可能觸發 expand 或 modify 決策",
            "ConceptInhibitor 可能產生 patch",
            "salience >= 0.5（觀點轉變有新穎性）",
        ],
        notes="刻意與 turn 3 的決策矛盾，測試矛盾偵測管線。",
    ),
    DialogueTurn(
        turn=8,
        user_input="所以最後的方案是混合架構：交易相關的留在 PostgreSQL，使用者行為日誌和內容資料搬到 MongoDB。",
        scenario="矛盾-解決",
        expected_behaviors=[
            "ConceptVerifier 可能觸發 expand（擴充概念為混合架構）",
            "概念提取：混合架構、Polyglot Persistence",
            "前面「資料庫遷移」概念可能被 modify",
        ],
        notes="矛盾解決，概念應該被更新而非新建。",
    ),

    # ── 重複 / 低 novelty（輪 9-10）──────────────────────────
    DialogueTurn(
        turn=9,
        user_input="對了，我昨天做的番茄炒蛋還蠻好吃的。",
        scenario="低novelty-重複",
        expected_behaviors=[
            "novelty 應該較低（重複 turn 2 話題）",
            "salience < 0.4（NEUTRAL 區域）",
            "episode 仍然存儲（無 salience 門閥）",
            "不應觸發 entity 提取",
        ],
        notes="回到 turn 2 的話題，測試低 novelty 偵測。",
    ),
    DialogueTurn(
        turn=10,
        user_input="嗯，番茄炒蛋就是那樣嘛，沒什麼特別的。",
        scenario="低novelty-極低",
        expected_behaviors=[
            "novelty 極低",
            "salience < 0.3",
            "episode 仍然存儲",
            "概念提取可能跳過（已有「番茄炒蛋」概念）",
        ],
        notes="連續低 novelty，驗證 salience 不做門閥。",
    ),

    # ── 高 uncertainty / 反思觸發（輪 11）──────────────────────
    DialogueTurn(
        turn=11,
        user_input="我不確定混合架構到底是不是正確的選擇。如果未來資料量暴增到十億級別，MongoDB 的 sharding 能不能撐住？還是應該考慮 Cassandra 或 DynamoDB？我真的很猶豫，完全不知道該怎麼決定。",
        scenario="高uncertainty-反思觸發",
        expected_behaviors=[
            "uncertainty > 0.7（大量猶豫用語）",
            "ReflectionModule.should_reflect() == True",
            "reflection.action 應為 'question' 或 'browse'",
            "salience >= 0.5（決策困難本身有意義）",
            "概念提取：Cassandra、DynamoDB、sharding",
        ],
        notes="刻意使用大量不確定語氣，觸發反思模組。正好是 turn 11（接近 10 的倍數）。",
    ),

    # ── 多義詞（輪 12-13）──────────────────────────────────
    DialogueTurn(
        turn=12,
        user_input="說到 Python，我最近養了一條球蟒當寵物。牠的花紋很漂亮，黑黃相間的。",
        scenario="多義詞-Python(蛇)",
        expected_behaviors=[
            "「Python」在這裡指蛇，不是程式語言",
            "ConceptExtractor 應偵測多義性",
            "若已有 Python(語言) 概念，similarity 應在 0.85-0.92",
            "應建立新概念或 RELATES_TO 邊，不應合併",
            "概念提取：球蟒、寵物",
        ],
        notes="經典多義詞場景。前面如果沒提到 Python 語言，這輪主要測概念命名衝突。",
    ),
    DialogueTurn(
        turn=13,
        user_input="Python 的飼養環境要控制在 26-32 度之間，濕度 50-60%。和寫 Python 程式比起來，照顧牠反而更花時間。",
        scenario="多義詞-Python(雙重)",
        expected_behaviors=[
            "同時出現兩個含義的 Python",
            "ConceptExtractor 應區分兩個概念",
            "多義詞三層分流邏輯觸發",
            "可能建立兩個不同的 concept 或標記多義性",
        ],
        notes="同一句話包含兩個 Python 含義，極端多義詞場景。",
    ),

    # ── 收尾 / Dream 前確認（輪 14-15）──────────────────────
    DialogueTurn(
        turn=14,
        user_input="總結一下今天的討論：資料庫決定用混合架構，PostgreSQL 處理交易、MongoDB 處理日誌和內容。然後我的球蟒需要買一個新的加熱墊。",
        scenario="收尾-總結",
        expected_behaviors=[
            "context.episodes 應包含多筆相關記憶",
            "檢索到資料庫相關 + 寵物相關的 episode",
            "salience 中等（總結性質）",
            "圖譜中應有完整的概念關係鏈",
        ],
        notes="收尾總結，驗證跨話題記憶檢索。",
    ),
    DialogueTurn(
        turn=15,
        user_input="好的，今天就到這裡吧。謝謝你的建議！",
        scenario="收尾-結束",
        expected_behaviors=[
            "salience 低（告別語）",
            "episode 正常存儲",
            "此輪結束後可觸發 Dream Engine",
        ],
        notes="最後一輪。之後手動觸發 Dream，觀察 DreamReport。",
    ),
]
# fmt: on


def get_script() -> list[DialogueTurn]:
    """取得完整對話劇本。"""
    return DIALOGUE_SCRIPT


def print_script_summary() -> None:
    """印出劇本摘要表。"""
    print(f"{'輪次':>4} │ {'場景':<20} │ {'預期行為數':>6} │ 使用者輸入")
    print("─" * 80)
    for t in DIALOGUE_SCRIPT:
        preview = t.user_input[:40] + ("…" if len(t.user_input) > 40 else "")
        print(f"{t.turn:>4} │ {t.scenario:<20} │ {len(t.expected_behaviors):>6} │ {preview}")


if __name__ == "__main__":
    print_script_summary()
