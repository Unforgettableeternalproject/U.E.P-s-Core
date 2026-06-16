"""
Pipeline Integration Tests — 驗證記憶系統各層的整合行為

測試策略：
  - 大多數測試不依賴 LLM（llm_fn=None 啟發式降級），快速可重現
  - test_concept_extractor_creates_concept 需要真實 LLM（標記 live_api）
  - 每個 test 使用 tmp_path 隔離存儲，確保完全獨立
  - Windows EPERM 清理問題以 try/except 包裝

APScheduler 關閉：在模組頂部設定環境變數，確保在 import echo_memory 前已就緒
"""

from __future__ import annotations

import os

# 必須在 import 任何 echo_memory 模組前設定，避免 APScheduler 啟動
os.environ.setdefault("DREAM_ENABLE_IDLE", "false")
os.environ.setdefault("DREAM_ENABLE_CRON", "false")

import shutil

import pytest

from echo_memory.affect.salience import SalienceEvaluator
from echo_memory.affect.signals import SalienceSignals, SubconsciousAssessment
from echo_memory.affect.soul_note import SoulNote, SoulNoteManager
from echo_memory.affect.subconscious import SubconsciousAssessor
from echo_memory.affect.reflection import ReflectionModule
from echo_memory.engine import MemoryEngine
from echo_memory.neurochem.state import NeurochemState


# ─── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def engine(tmp_path):
    """建立隔離的 MemoryEngine，測試結束後嘗試清理。"""
    eng = MemoryEngine(storage_path=tmp_path)
    yield eng
    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


@pytest.fixture
def soul_note_manager(tmp_path):
    """建立隔離的 SoulNoteManager。"""
    manager = SoulNoteManager(storage_path=tmp_path)
    yield manager
    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


# ─── 測試 1：完整對話輪次存儲 Episode ──────────────────────────────────────


@pytest.mark.slow
def test_full_turn_stores_episode(tmp_path):
    """
    驗證 store_episode() 正確寫入情節記憶的所有欄位。

    不依賴 LLM，純測存儲層行為：
    - episode_id 格式（ep_ 開頭）
    - 內部快取 _episodes 有此筆記錄
    - user_input / agent_response / session_id 正確保存
    - embedding 非空（SentenceTransformer 正常運作）
    - salience_score 在合法範圍 [0, 1]
    """
    engine = MemoryEngine(storage_path=tmp_path)

    user_input = "請說明什麼是遞迴函式"
    agent_response = "遞迴函式是一個在執行過程中呼叫自身的函式，需要有基本情況（base case）才能終止。"
    session_id = "test-session-001"

    episode_id = engine.store_episode(
        user_input=user_input,
        agent_response=agent_response,
        session_id=session_id,
        salience=0.7,
    )

    # episode_id 格式正確
    assert episode_id.startswith("ep_"), (
        f"episode_id 應以 'ep_' 開頭，實際為 '{episode_id}'"
    )

    # 內部快取有此筆
    episodes = engine.episodic._episodes
    assert episode_id in episodes, "episodic._episodes 中找不到剛存入的 episode"

    ep = episodes[episode_id]

    # 欄位正確保存
    assert ep.user_input == user_input, "user_input 不符"
    assert ep.agent_response == agent_response, "agent_response 不符"
    assert ep.session_id == session_id, "session_id 不符"

    # embedding 非空（SentenceTransformer 正常生成向量）
    assert len(ep.embedding) > 0, "embedding 應為非空 list"

    # salience_score 在合法範圍
    assert 0.0 <= ep.salience_score <= 1.0, (
        f"salience_score 應在 [0, 1]，實際為 {ep.salience_score}"
    )

    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


# ─── 測試 2：SubconsciousAssessor 五維度評估 ──────────────────────────────


@pytest.mark.slow
def test_subconscious_five_dims():
    """
    驗證 SubconsciousAssessor 在 llm_fn=None 啟發式降級模式下，
    回傳正確的 SubconsciousAssessment，五個維度都在合法範圍內。

    不依賴 LLM，測試啟發式降級邏輯覆蓋不同輸入類型：
    - 長文本 vs 短文本
    - 問句（含問號）vs 陳述句
    """
    assessor = SubconsciousAssessor(llm_fn=None)

    def _assert_dims_valid(result: SubconsciousAssessment, label: str) -> None:
        """驗證五個維度都在合法範圍。"""
        assert -1.0 <= result.emotional_tone <= 1.0, (
            f"[{label}] emotional_tone={result.emotional_tone} 超出 [-1.0, 1.0]"
        )
        assert 0.0 <= result.novelty <= 1.0, (
            f"[{label}] novelty={result.novelty} 超出 [0.0, 1.0]"
        )
        assert 0.0 <= result.complexity <= 1.0, (
            f"[{label}] complexity={result.complexity} 超出 [0.0, 1.0]"
        )
        assert 0.0 <= result.uncertainty <= 1.0, (
            f"[{label}] uncertainty={result.uncertainty} 超出 [0.0, 1.0]"
        )
        assert 0.0 <= result.dream_resonance <= 1.0, (
            f"[{label}] dream_resonance={result.dream_resonance} 超出 [0.0, 1.0]"
        )

    # 測試案例 1：長文本問句（高複雜度，多問號）
    long_question = (
        "我想了解深度學習中的 Transformer 架構，"
        "它的 Self-Attention 機制是怎麼運作的？"
        "和 RNN 的主要差異是什麼？為什麼它在 NLP 領域表現更好？"
    )
    result_long = assessor.assess(user_input=long_question)
    assert isinstance(result_long, SubconsciousAssessment)
    _assert_dims_valid(result_long, "長文本問句")

    # 長問句應有較高複雜度（長度比短句更長）
    assert result_long.complexity > 0.0, "長文本問句的 complexity 應大於 0"

    # 問句應有不確定性（問號數量 > 0 → uncertainty > 0）
    assert result_long.uncertainty > 0.0, "含問號的問句應有非零 uncertainty"

    # 測試案例 2：短陳述句（低複雜度）
    short_statement = "天氣很好。"
    result_short = assessor.assess(user_input=short_statement)
    assert isinstance(result_short, SubconsciousAssessment)
    _assert_dims_valid(result_short, "短陳述句")

    # 短句複雜度應低於長句
    assert result_short.complexity < result_long.complexity, (
        "短陳述句的 complexity 應低於長問句"
    )

    # 無問號 → uncertainty 為 0
    assert result_short.uncertainty == 0.0, "無問號的陳述句 uncertainty 應為 0"

    # 測試案例 3：包含感嘆號（emotional_tone 正向）
    excited_input = "太棒了！這個功能正是我需要的！"
    result_excited = assessor.assess(user_input=excited_input)
    assert isinstance(result_excited, SubconsciousAssessment)
    _assert_dims_valid(result_excited, "感嘆句")
    # 啟發式：感嘆號 → tone 為正值
    assert result_excited.emotional_tone >= 0.0, (
        "含感嘆號的句子 emotional_tone 應 >= 0"
    )


# ─── 測試 3：SalienceEvaluator 三值輸出 ────────────────────────────────────


@pytest.mark.slow
def test_salience_three_values():
    """
    驗證 SalienceEvaluator.evaluate() 回傳的三值都在 [0.0, 1.0]。

    測試不同的 SalienceSignals 組合：
    - 高 complexity + 高 novelty（高顯著性期待）
    - 有正向 feedback（影響 da_weight）
    - 發生錯誤（error_bonus 加分，da_weight 降分）
    - 基本預設值（中性）
    """
    neurochem = NeurochemState(dopamine=0.5, serotonin=0.5)
    evaluator = SalienceEvaluator(neurochem=neurochem)

    def _assert_three_values(
        result: tuple[float, float, float],
        label: str,
    ) -> None:
        """驗證三值都在 [0.0, 1.0]。"""
        salience, da_weight, ht_weight = result
        assert 0.0 <= salience <= 1.0, (
            f"[{label}] salience={salience} 超出 [0.0, 1.0]"
        )
        assert 0.0 <= da_weight <= 1.0, (
            f"[{label}] da_weight={da_weight} 超出 [0.0, 1.0]"
        )
        assert 0.0 <= ht_weight <= 1.0, (
            f"[{label}] ht_weight={ht_weight} 超出 [0.0, 1.0]"
        )

    # 案例 1：高 complexity + 高 novelty（預期高 salience）
    high_signals = SalienceSignals(
        task_complexity=0.9,
        novelty_score=0.9,
        user_feedback=0.0,
        error_occurred=False,
        verification_score=1.0,
    )
    result_high = evaluator.evaluate(high_signals)
    _assert_three_values(result_high, "高 complexity+novelty")

    # 高輸入應產生高 salience
    assert result_high[0] > 0.5, (
        f"高 complexity+novelty 的 salience 應 > 0.5，實際 {result_high[0]}"
    )

    # 案例 2：正向 feedback（da_weight 應受影響）
    feedback_signals = SalienceSignals(
        task_complexity=0.5,
        novelty_score=0.5,
        user_feedback=0.8,  # 正向回饋
        error_occurred=False,
        verification_score=1.0,
    )
    result_feedback = evaluator.evaluate(feedback_signals)
    _assert_three_values(result_feedback, "正向 feedback")

    # da_weight 應高於中性狀態（0.5 + 0.8*0.3 = 0.74，但會被 clamp）
    neutral_da = neurochem.dopamine
    assert result_feedback[1] >= neutral_da, (
        "正向 feedback 的 da_weight 應 >= 當前 dopamine"
    )

    # 案例 3：發生錯誤（error_bonus 加分，da_weight 降分）
    error_signals = SalienceSignals(
        task_complexity=0.5,
        novelty_score=0.5,
        user_feedback=0.0,
        error_occurred=True,
        verification_score=0.3,
    )
    result_error = evaluator.evaluate(error_signals)
    _assert_three_values(result_error, "發生錯誤")

    # 錯誤時 da_weight 應低於中性
    assert result_error[1] < neutral_da, (
        f"發生錯誤的 da_weight 應 < {neutral_da}，實際 {result_error[1]}"
    )

    # 案例 4：預設中性值
    default_signals = SalienceSignals()
    result_default = evaluator.evaluate(default_signals)
    _assert_three_values(result_default, "預設中性")


# ─── 測試 4：ConceptExtractor 背景概念提取 ──────────────────────────────────


@pytest.mark.live_api
@pytest.mark.slow
def test_concept_extractor_creates_concept(tmp_path, require_live_api):
    """
    驗證 ConceptExtractor 在 LLM 可用時，能在背景提取概念並寫入 SemanticMemory。

    需要真實 Gemini LLM（標記 live_api），若無 API key 則自動 skip。

    驗證目標：
    - 背景提取完成後，semantic.list_concepts() 非空
    - 至少一個 concept 的 source_episode_ids 包含此 episode_id
    """
    from llm_integration.llm_client import LLMClient
    from echo_memory.affect.concept_extractor import ConceptExtractor

    # 建立 MemoryEngine
    engine = MemoryEngine(storage_path=tmp_path, llm_fn=None)

    # 存一筆高 salience episode（做為提取來源）
    episode_id = engine.store_episode(
        user_input="請解釋神經網路的反向傳播算法",
        agent_response=(
            "反向傳播（Backpropagation）是一種透過計算梯度來訓練神經網路的算法。"
            "它利用鏈式法則從輸出層向輸入層傳遞誤差，更新每個權重。"
        ),
        session_id="concept-test-session",
        salience=0.9,
    )

    # 取得剛存入的 episode
    ep = engine.episodic._episodes[episode_id]

    # 建立 LLM client 並初始化 ConceptExtractor
    try:
        llm_client = LLMClient()
        llm_fn = llm_client.generate
    except Exception as exc:
        pytest.skip(f"無法初始化 LLMClient: {exc}")

    extractor = ConceptExtractor(
        llm_fn=llm_fn,
        semantic=engine.semantic,
        graph=engine.episodic.graph,
        embed_fn=lambda text: engine.episodic._generate_embedding(text),
        inhibitor=None,
    )

    # 在背景提取概念
    future = extractor.extract_in_background(
        episode=ep,
        salience=0.9,
        concept_seeds=None,
    )

    # 等待提取完成
    completed = extractor.flush(timeout=30)
    assert completed >= 1, f"flush 回報 {completed} 個完成，預期 >= 1"

    # 關閉執行緒池
    extractor.shutdown(wait=False)

    # 驗證 SemanticMemory 有概念寫入
    concepts = engine.semantic.list_concepts()
    assert len(concepts) > 0, "提取後 semantic.list_concepts() 應非空"

    # 至少一個 concept 包含此 episode_id 在 source_episode_ids
    matched = [
        c for c in concepts
        if episode_id in c.source_episode_ids
    ]
    assert len(matched) > 0, (
        f"應有至少一個 concept 的 source_episode_ids 包含 '{episode_id}'，"
        f"目前有 {len(concepts)} 個 concept 但都不包含"
    )

    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


# ─── 測試 5：NeurochemState 持久化 ─────────────────────────────────────────


@pytest.mark.slow
def test_neurochem_persists(tmp_path):
    """
    驗證 NeurochemState 能正確持久化並在重載時還原。

    流程：
    1. 建立 MemoryEngine，設定 dopamine = 0.8
    2. 呼叫 _save_neurochem() 寫入磁碟
    3. 建立新的 MemoryEngine（同一 tmp_path）
    4. 驗證 dopamine ≈ 0.8（允許微小誤差，因載入時可能套用自然衰減）
    """
    engine_a = MemoryEngine(storage_path=tmp_path)

    # 設定特定的 dopamine 值
    engine_a.neurochem.dopamine = 0.8
    engine_a._save_neurochem()

    # 重新建立 MemoryEngine（同路徑），應載入已存儲的狀態
    engine_b = MemoryEngine(storage_path=tmp_path)

    # 允許 ±0.1 的誤差（_load_neurochem 會套用時間衰減，但測試中時間差極短）
    # 通常衰減條件是 elapsed_hours > 0.1，即 6 分鐘，測試不會超過這個
    assert abs(engine_b.neurochem.dopamine - 0.8) < 0.1, (
        f"重載後 dopamine 應接近 0.8，實際 {engine_b.neurochem.dopamine}"
    )

    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


# ─── 測試 6：SoulNote 寫入與讀取 ────────────────────────────────────────────


@pytest.mark.slow
def test_soul_note_written(tmp_path):
    """
    驗證 SoulNoteManager 能正確寫入並讀回 SoulNote。

    驗證目標：
    - get_notes_today() 回傳至少 1 筆
    - 最後一筆的 category 與預期相符
    - metadata 中的自訂欄位正確保存
    """
    manager = SoulNoteManager(storage_path=tmp_path)

    # 寫入第一筆：反思類型
    note_reflection = SoulNote(
        category="reflection",
        content="今日對話中發現使用者對遞迴理解不深，值得後續追蹤",
        metadata={"session_id": "test-session-001", "turn": 3},
        tags=["reflection", "recursion"],
    )
    manager.add_note(note_reflection)

    # 寫入第二筆：發現類型
    note_discovery = SoulNote(
        category="discovery",
        content="使用者提到了 Transformer 架構，這是一個重要的新話題",
        metadata={"topic": "transformer", "confidence": 0.9},
        tags=["discovery", "transformer"],
    )
    manager.add_note(note_discovery)

    # 讀取今日筆記
    notes_today = manager.get_notes_today()

    assert len(notes_today) >= 1, (
        f"get_notes_today() 應回傳至少 1 筆，實際 {len(notes_today)} 筆"
    )

    # 驗證最後一筆（discovery）
    last_note = notes_today[-1]
    assert last_note.category == "discovery", (
        f"最後一筆的 category 應為 'discovery'，實際為 '{last_note.category}'"
    )
    assert last_note.metadata.get("topic") == "transformer", (
        "最後一筆的 metadata['topic'] 應為 'transformer'"
    )
    assert last_note.metadata.get("confidence") == 0.9, (
        "最後一筆的 metadata['confidence'] 應為 0.9"
    )

    # 驗證兩筆都正確寫入
    assert len(notes_today) >= 2, (
        f"應有至少 2 筆，實際 {len(notes_today)} 筆"
    )
    assert notes_today[0].category == "reflection", (
        f"第一筆的 category 應為 'reflection'，實際為 '{notes_today[0].category}'"
    )
    assert "recursion" in notes_today[0].tags, "第一筆應有 'recursion' tag"

    try:
        shutil.rmtree(tmp_path, ignore_errors=True)
    except Exception:
        pass


# ─── 測試 7：ReflectionModule 不確定性觸發反思 ────────────────────────────


@pytest.mark.slow
def test_reflection_triggers_on_uncertainty():
    """
    驗證 ReflectionModule.should_reflect() 在不確定性超過閾值時觸發反思。

    啟發式降級模式（llm_fn=None），不依賴 LLM。

    驗證目標：
    - uncertainty=0.8（> 0.7 閾值）→ should_reflect() 回傳 True
    - uncertainty=0.3（< 0.7 閾值）且未到達輪次間隔 → 回傳 False
    - uncertainty=0.0 情況也驗證

    注意：每次呼叫 should_reflect() 都會遞增 _turn_counter，
    使用不同的 ReflectionModule 實例隔離副作用。
    """
    # 案例 1：高不確定性 → 應觸發
    reflection_high = ReflectionModule(llm_fn=None, reflect_interval=10)
    result_high = reflection_high.should_reflect(uncertainty=0.8)
    assert result_high is True, (
        "uncertainty=0.8 > 0.7，should_reflect() 應回傳 True"
    )

    # 案例 2：低不確定性，第一次呼叫 → 不到達間隔（_turn_counter=1，不是 10 的倍數）
    reflection_low = ReflectionModule(llm_fn=None, reflect_interval=10)
    result_low = reflection_low.should_reflect(uncertainty=0.3)
    assert result_low is False, (
        "uncertainty=0.3 < 0.7，且未到達輪次間隔，should_reflect() 應回傳 False"
    )

    # 案例 3：uncertainty=0.0 → 同樣不觸發（第一次呼叫，turn_counter=1）
    reflection_zero = ReflectionModule(llm_fn=None, reflect_interval=10)
    result_zero = reflection_zero.should_reflect(uncertainty=0.0)
    assert result_zero is False, (
        "uncertainty=0.0，且未到達輪次間隔，should_reflect() 應回傳 False"
    )

    # 案例 4：恰好達到輪次間隔（第 10 次呼叫）→ 觸發
    reflection_interval = ReflectionModule(llm_fn=None, reflect_interval=10)
    for _ in range(9):  # 前 9 次，_turn_counter 累積到 9
        reflection_interval.should_reflect(uncertainty=0.0)
    # 第 10 次呼叫，_turn_counter=10，10 % 10 == 0 → True
    result_interval = reflection_interval.should_reflect(uncertainty=0.0)
    assert result_interval is True, (
        "第 10 次呼叫（_turn_counter=10）應觸發輪次反思"
    )

    # 案例 5：邊界值 uncertainty=0.7（不超過閾值，> 0.7 才觸發）
    reflection_boundary = ReflectionModule(llm_fn=None, reflect_interval=10)
    result_boundary = reflection_boundary.should_reflect(uncertainty=0.7)
    assert result_boundary is False, (
        "uncertainty=0.7 不超過閾值（需 > 0.7），should_reflect() 應回傳 False"
    )
