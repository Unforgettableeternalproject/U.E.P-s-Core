"""
NeurochemState 測試 — Phase 2 Branch 4 驗收

驗收標準：
  1. 不同 DA/5-HT 值產生不同檢索行為（衍生參數不同）
  2. 5 種模式正確判定
  3. 漸進式刺激（不直接跳變）
  4. 自然衰減回到基線
  5. EcphoryRetrieval 接受 neurochem 參數
"""

from __future__ import annotations

import pytest

from echo_memory.neurochem.state import NeurochemMode, NeurochemState

# ─── 模式判定 ──────────────────────────────────────────────────────


class TestNeurochemMode:
    """5 種模式的正確判定。"""

    def test_balanced(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        assert s.mode == NeurochemMode.BALANCED

    def test_high_da(self):
        s = NeurochemState(dopamine=0.8, serotonin=0.5)
        assert s.mode == NeurochemMode.HIGH_DA

    def test_high_5ht(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.8)
        assert s.mode == NeurochemMode.HIGH_5HT

    def test_excited(self):
        """EXCITED = DA 高 + 5-HT 低（衝動探索，對齊 OpenSoul）。"""
        s = NeurochemState(dopamine=0.8, serotonin=0.3)
        assert s.mode == NeurochemMode.EXCITED

    def test_cautious(self):
        """CAUTIOUS = DA 低 + 5-HT 高（保守深思，對齊 OpenSoul）。"""
        s = NeurochemState(dopamine=0.2, serotonin=0.7)
        assert s.mode == NeurochemMode.CAUTIOUS

    def test_both_high_is_high_da(self):
        """DA 和 5-HT 都高時，EXCITED 不觸發（5-HT >= 0.4），走 HIGH_DA。"""
        s = NeurochemState(dopamine=0.8, serotonin=0.8)
        assert s.mode == NeurochemMode.HIGH_DA

    def test_both_low_is_balanced(self):
        """DA 和 5-HT 都低但未到各自閾值時，走 BALANCED。"""
        s = NeurochemState(dopamine=0.4, serotonin=0.4)
        assert s.mode == NeurochemMode.BALANCED

    def test_boundary_excited(self):
        """剛好在 EXCITED 邊界：DA=0.75, 5-HT=0.39。"""
        s = NeurochemState(dopamine=0.75, serotonin=0.39)
        assert s.mode == NeurochemMode.EXCITED

    def test_boundary_cautious(self):
        """剛好在 CAUTIOUS 邊界：DA=0.34, 5-HT=0.65。"""
        s = NeurochemState(dopamine=0.34, serotonin=0.65)
        assert s.mode == NeurochemMode.CAUTIOUS


# ─── 漸進式刺激 ────────────────────────────────────────────────────


class TestStimulation:
    """漸進式刺激行為。"""

    def test_stimulate_increases_da(self):
        s = NeurochemState(dopamine=0.5)
        s.stimulate(dopamine_delta=0.3)
        assert s.dopamine > 0.5
        # 漸進式，不會直接跳到 0.8
        assert s.dopamine < 0.8

    def test_stimulate_decreases_5ht(self):
        s = NeurochemState(serotonin=0.5)
        s.stimulate(serotonin_delta=-0.3)
        assert s.serotonin < 0.5

    def test_stimulate_clamped_at_1(self):
        s = NeurochemState(dopamine=0.9)
        s.stimulate(dopamine_delta=0.5)
        assert s.dopamine <= 1.0

    def test_stimulate_clamped_at_0(self):
        s = NeurochemState(dopamine=0.1)
        s.stimulate(dopamine_delta=-0.5)
        assert s.dopamine >= 0.0

    def test_repeated_stimulation_approaches_target(self):
        """多次刺激逐步接近目標。"""
        s = NeurochemState(dopamine=0.5, approach_rate=0.3)
        for _ in range(10):
            s.stimulate(dopamine_delta=0.5)
        # 10 次刺激後應該接近但不超過 1.0
        assert s.dopamine > 0.8
        assert s.dopamine <= 1.0

    def test_zero_delta_no_change(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        s.stimulate(dopamine_delta=0.0, serotonin_delta=0.0)
        assert s.dopamine == 0.5
        assert s.serotonin == 0.5


# ─── 自然衰減 ──────────────────────────────────────────────────────


class TestDecay:
    """tick() 自然衰減。"""

    def test_decay_towards_baseline(self):
        s = NeurochemState(dopamine=0.9, baseline_da=0.5, decay_rate=0.1)
        s.tick()
        assert s.dopamine < 0.9
        assert s.dopamine > 0.5  # 還沒回到基線

    def test_many_ticks_approach_baseline(self):
        s = NeurochemState(dopamine=0.9, baseline_da=0.5, decay_rate=0.1)
        for _ in range(100):
            s.tick()
        assert abs(s.dopamine - 0.5) < 0.01  # 100 次後非常接近基線

    def test_decay_from_below_baseline(self):
        """低於基線時也會回升。"""
        s = NeurochemState(dopamine=0.1, baseline_da=0.5, decay_rate=0.1)
        s.tick()
        assert s.dopamine > 0.1

    def test_at_baseline_no_change(self):
        s = NeurochemState(dopamine=0.5, baseline_da=0.5, decay_rate=0.1)
        s.tick()
        assert s.dopamine == pytest.approx(0.5)


# ─── 衍生檢索參數 ──────────────────────────────────────────────────


class TestRetrievalParams:
    """不同神經化學狀態產生不同檢索參數。"""

    def test_high_da_wider_search(self):
        """高 DA → seed_k 更多、min_weight 更低。"""
        balanced = NeurochemState(dopamine=0.5, serotonin=0.5)
        high_da = NeurochemState(dopamine=0.9, serotonin=0.5)

        p_balanced = balanced.get_retrieval_params()
        p_high_da = high_da.get_retrieval_params()

        assert p_high_da.seed_k > p_balanced.seed_k
        assert p_high_da.min_weight < p_balanced.min_weight

    def test_high_5ht_deeper_search(self):
        """高 5-HT → max_hops 更深。"""
        balanced = NeurochemState(dopamine=0.5, serotonin=0.5)
        high_5ht = NeurochemState(dopamine=0.5, serotonin=0.9)

        p_balanced = balanced.get_retrieval_params()
        p_high_5ht = high_5ht.get_retrieval_params()

        assert p_high_5ht.max_hops >= p_balanced.max_hops

    def test_cautious_narrow_search(self):
        """CAUTIOUS 模式 → 最保守的參數。"""
        cautious = NeurochemState(dopamine=0.1, serotonin=0.1)
        p = cautious.get_retrieval_params()

        assert p.seed_k <= 4
        assert p.max_hops == 1
        assert p.min_weight > 0.2

    def test_excited_wide_search(self):
        """EXCITED 模式（DA 高 + 5-HT 低）→ 廣但淺的搜索。"""
        excited = NeurochemState(dopamine=0.9, serotonin=0.2)
        p = excited.get_retrieval_params()

        assert p.seed_k >= 7         # DA 高 → 種子多
        assert p.max_hops == 1       # 5-HT 低 → 跳數淺
        assert p.min_weight < 0.1    # DA 高 → 閾值低

    def test_both_high_maximum_search(self):
        """DA 和 5-HT 都高 → 最大搜索範圍（種子多 + 跳數深）。"""
        both_high = NeurochemState(dopamine=0.9, serotonin=0.9)
        p = both_high.get_retrieval_params()

        assert p.seed_k >= 7
        assert p.max_hops == 3
        assert p.min_weight < 0.1

    def test_params_values_in_valid_range(self):
        """所有參數值在合理範圍內。"""
        for da in [0.0, 0.3, 0.5, 0.7, 1.0]:
            for ht in [0.0, 0.3, 0.5, 0.7, 1.0]:
                s = NeurochemState(dopamine=da, serotonin=ht)
                p = s.get_retrieval_params()
                assert 3 <= p.seed_k <= 8      # int(3 + 5 * da), da ∈ [0,1]
                assert 1 <= p.max_hops <= 3
                assert 0.0 <= p.min_weight <= 0.35
                assert 3 <= p.concept_top_k <= 8  # int(3 + 5 * da)
                assert 0.0 <= p.concept_min_similarity <= 1.0


# ─── 序列化 ────────────────────────────────────────────────────────


class TestSerialization:
    def test_round_trip(self):
        s = NeurochemState(dopamine=0.7, serotonin=0.3)
        d = s.to_dict()
        s2 = NeurochemState.from_dict(d)
        assert s2.dopamine == pytest.approx(0.7)
        assert s2.serotonin == pytest.approx(0.3)
        assert s2.mode == s.mode

    def test_to_dict_includes_mode(self):
        s = NeurochemState(dopamine=0.8, serotonin=0.3)
        d = s.to_dict()
        assert d["mode"] == "excited"

    def test_to_dict_includes_derived_properties(self):
        """序列化包含衍生屬性（verification_threshold 等）。"""
        s = NeurochemState(dopamine=0.7, serotonin=0.6)
        d = s.to_dict()
        assert "verification_threshold" in d
        assert "search_breadth" in d
        assert "learning_rate" in d
        assert "salience_boost" in d


# ─── 事件驅動方法（OpenSoul 對齊）─────────────────────────────────


class TestEventDrivenMethods:
    """OpenSoul 事件驅動方法的行為驗證。"""

    def test_on_success_increases_da(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        s.on_success(reward=0.3)
        assert s.dopamine > 0.5

    def test_on_failure_decreases_da_increases_ht(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        s.on_failure(penalty=0.2)
        assert s.dopamine < 0.5
        assert s.serotonin > 0.5

    def test_on_uncertainty_increases_ht(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        s.on_uncertainty(level=0.5)
        assert s.serotonin > 0.5

    def test_on_discovery_increases_da(self):
        s = NeurochemState(dopamine=0.5, serotonin=0.5)
        s.on_discovery(novelty=0.6)
        assert s.dopamine > 0.5
        # 5-HT 不變
        assert s.serotonin == pytest.approx(0.5)

    def test_natural_decay_towards_baseline(self):
        s = NeurochemState(dopamine=0.9, serotonin=0.1,
                           baseline_da=0.5, baseline_5ht=0.5)
        s.natural_decay(hours=10.0)
        assert s.dopamine < 0.9
        assert s.serotonin > 0.1

    def test_natural_decay_clamped(self):
        s = NeurochemState(dopamine=0.9, serotonin=0.1)
        s.natural_decay(hours=1000.0)
        assert 0.0 <= s.dopamine <= 1.0
        assert 0.0 <= s.serotonin <= 1.0

    def test_verification_threshold_rises_with_serotonin(self):
        low = NeurochemState(serotonin=0.1)
        high = NeurochemState(serotonin=0.9)
        assert high.verification_threshold > low.verification_threshold

    def test_salience_boost_rises_with_dopamine(self):
        low = NeurochemState(dopamine=0.1)
        high = NeurochemState(dopamine=0.9)
        assert high.salience_boost > low.salience_boost


# ─── EcphoryRetrieval 整合 ──────────────────────────────────────────


class TestEcphoryNeurochemIntegration:
    """EcphoryRetrieval 接受 neurochem 參數。"""

    def test_retrieve_with_neurochem_does_not_crash(self, tmp_path):
        """傳入 NeurochemState 不崩潰。"""
        from echo_memory.retrieval.ecphory import EcphoryRetrieval
        from echo_memory.storage.episodic import EpisodicMemory
        from echo_memory.storage.semantic import SemanticMemory

        ep_dir = tmp_path / "ep"
        sem_dir = tmp_path / "sem"
        ep_dir.mkdir()
        sem_dir.mkdir()

        episodic = EpisodicMemory(storage_path=ep_dir)
        semantic = SemanticMemory(storage_path=sem_dir)
        retrieval = EcphoryRetrieval(episodic=episodic, semantic=semantic)

        state = NeurochemState(dopamine=0.8, serotonin=0.3)
        context = retrieval.retrieve("test", neurochem=state, min_similarity=0.0)

        # 不崩潰即通過
        assert isinstance(context.episodes, list)

    def test_different_neurochem_different_behavior(self, tmp_path):
        """不同神經化學狀態至少不產生完全相同的內部邏輯。"""
        state1 = NeurochemState(dopamine=0.1, serotonin=0.1)
        state2 = NeurochemState(dopamine=0.9, serotonin=0.9)

        p1 = state1.get_retrieval_params()
        p2 = state2.get_retrieval_params()

        # 至少有一個參數不同
        assert (
            p1.seed_k != p2.seed_k
            or p1.max_hops != p2.max_hops
            or p1.min_weight != p2.min_weight
        )
