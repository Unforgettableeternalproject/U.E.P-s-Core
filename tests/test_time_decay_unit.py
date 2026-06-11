"""
test_time_decay_unit.py — 時間衰減單位一致性驗證測試

Issue #3 修復驗收測試：確認 Recency 衰減使用小時為單位，
行為符合 exp(-0.01·hours) 公式預期。
"""

from __future__ import annotations

import math
import sys
import time
import types
from datetime import datetime
from unittest.mock import MagicMock

import pytest

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 在任何 echo_memory.dream import 之前注入假 apscheduler，
# 避免 DreamEngine 依賴炸掉整個 dream 套件的 import 鏈。
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _inject_mock_apscheduler() -> None:
    """把 apscheduler 全系列塞進 sys.modules（僅在未安裝時）。"""
    if "apscheduler" in sys.modules:
        return

    def _make_pkg(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        m.__path__ = []          # 讓 Python 視為 package
        m.__package__ = name
        sys.modules[name] = m
        return m

    pkg = _make_pkg("apscheduler")

    schedulers = _make_pkg("apscheduler.schedulers")
    bg = types.ModuleType("apscheduler.schedulers.background")
    bg.BackgroundScheduler = MagicMock
    sys.modules["apscheduler.schedulers.background"] = bg
    schedulers.background = bg

    triggers = _make_pkg("apscheduler.triggers")
    for sub, attr in [("cron", "CronTrigger"), ("interval", "IntervalTrigger")]:
        m = types.ModuleType(f"apscheduler.triggers.{sub}")
        setattr(m, attr, MagicMock)
        sys.modules[f"apscheduler.triggers.{sub}"] = m
        setattr(triggers, sub, m)

    pkg.schedulers = schedulers
    pkg.triggers = triggers


_inject_mock_apscheduler()

from echo_memory.dream.pruning import GraphPruning  # noqa: E402
from echo_memory.storage.graph import _SECONDS_PER_HOUR, MemoryGraph  # noqa: E402

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Fixtures
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def tmp_graph(tmp_path):
    """建立一個使用暫存目錄的 MemoryGraph 實例"""
    return MemoryGraph(storage_path=tmp_path)


def _make_episode_mock(ep_id: str, timestamp_iso: str, salience: float = 0.5):
    """建立最小 Episode mock"""
    ep = MagicMock()
    ep.id = ep_id
    ep.timestamp = datetime.fromisoformat(timestamp_iso)
    ep.salience_score = salience
    ep.session_id = "test-session"
    return ep


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 衰減公式基礎驗證（不依賴 graph，直接驗數學）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestDecayFormulaMath:
    """驗證 exp(-λ·hours) 公式在各時間點的數值正確性（λ=0.01）"""

    LAMBDA = 0.01

    def _decay(self, hours: float) -> float:
        return math.exp(-self.LAMBDA * hours)

    def test_1_hour(self):
        """1 小時後 recency ≈ 0.990"""
        result = self._decay(1)
        assert abs(result - 0.990) < 0.001, f"1hr decay={result:.4f}, expected ≈0.990"

    def test_24_hours(self):
        """24 小時後 recency ≈ 0.787"""
        result = self._decay(24)
        assert abs(result - 0.787) < 0.001, f"24hr decay={result:.4f}, expected ≈0.787"

    def test_7_days(self):
        """7 天（168 小時）後 recency ≈ 0.186"""
        result = self._decay(168)
        assert abs(result - 0.186) < 0.002, f"7day decay={result:.4f}, expected ≈0.186"

    def test_5_minutes_should_be_near_1(self):
        """5 分鐘後 recency 應接近 1.0（修復前用秒計算會導致 ~0.05）"""
        hours = 5 / 60  # 5 分鐘 = 5/60 小時
        result = self._decay(hours)
        assert result > 0.99, (
            f"5 分鐘後 recency={result:.4f}，應 > 0.99。"
            f"若此值接近 0.05，代表仍使用秒計算（Issue #3 未修復）"
        )

    def test_1_hour_should_be_near_1(self):
        """1 小時後 recency 應接近 1.0（合理範圍內）"""
        result = self._decay(1)
        assert result > 0.98, f"1hr recency={result:.4f}，應 > 0.98"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MemoryGraph._compute_recency 單元測試
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestComputeRecency:
    """直接測試 MemoryGraph._compute_recency（接受小時）"""

    def test_zero_hours_returns_one(self, tmp_graph):
        result = tmp_graph._compute_recency(0.0)
        assert result == pytest.approx(1.0)

    def test_1_hour(self, tmp_graph):
        result = tmp_graph._compute_recency(1.0)
        assert result == pytest.approx(0.9900, abs=0.001)

    def test_24_hours(self, tmp_graph):
        result = tmp_graph._compute_recency(24.0)
        assert result == pytest.approx(0.7866, abs=0.001)

    def test_168_hours_7_days(self, tmp_graph):
        result = tmp_graph._compute_recency(168.0)
        assert result == pytest.approx(0.1864, abs=0.002)

    def test_5_minutes_uses_hours_not_seconds(self, tmp_graph):
        """5 分鐘 = 1/12 小時；若傳入秒（300）衰減會過大"""
        hours = 5 / 60
        result = tmp_graph._compute_recency(hours)
        # 正確（小時）：exp(-0.01 * 0.0833) ≈ 0.9992
        # 錯誤（秒）：exp(-0.01 * 300) ≈ 0.0498
        assert result > 0.99, (
            f"5 分鐘後 recency={result:.4f}，正確應 > 0.99，"
            f"若接近 0.05 代表傳入的是秒而非小時"
        )

    def test_returns_value_in_0_1_range(self, tmp_graph):
        for hours in [0, 1, 24, 168, 720]:
            r = tmp_graph._compute_recency(hours)
            assert 0.0 <= r <= 1.0, f"hours={hours} 時 recency={r} 超出 [0,1] 範圍"

    def test_monotonically_decreasing(self, tmp_graph):
        """時間越長，recency 應越小"""
        values = [tmp_graph._compute_recency(h) for h in [0, 1, 24, 168, 720]]
        for i in range(len(values) - 1):
            assert values[i] > values[i + 1], (
                f"recency 非單調遞減：values={values}"
            )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# add_precedes_edge 整合測試：驗證建邊時換算正確
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestAddPrecedesEdgeTimeConversion:
    """確認 add_precedes_edge 傳入秒數，但內部以小時計算 recency"""

    def test_5_minutes_time_delta_gives_high_recency(self, tmp_graph):
        """5 分鐘前（300 秒）建立的邊，recency 應接近 1.0"""
        ep_a = _make_episode_mock("ep-a", "2026-01-01T00:00:00+00:00")
        ep_b = _make_episode_mock("ep-b", "2026-01-01T00:05:00+00:00")
        tmp_graph.add_episode_node(ep_a)
        tmp_graph.add_episode_node(ep_b)

        time_delta_seconds = 5 * 60  # 300 秒
        tmp_graph.add_precedes_edge("ep-a", "ep-b", time_delta=time_delta_seconds)

        edge_data = tmp_graph.graph["ep-a"]["ep-b"]
        recency = edge_data["recency_score"]

        assert recency > 0.99, (
            f"5 分鐘時間差的 recency={recency:.4f}，應 > 0.99。"
            f"若接近 0.05，代表仍以秒計算衰減（Issue #3）"
        )

    def test_1_hour_time_delta(self, tmp_graph):
        """1 小時前（3600 秒）建立的邊，recency 應 ≈ 0.990"""
        ep_a = _make_episode_mock("ep-c", "2026-01-01T00:00:00+00:00")
        ep_b = _make_episode_mock("ep-d", "2026-01-01T01:00:00+00:00")
        tmp_graph.add_episode_node(ep_a)
        tmp_graph.add_episode_node(ep_b)

        time_delta_seconds = 3600  # 1 小時
        tmp_graph.add_precedes_edge("ep-c", "ep-d", time_delta=time_delta_seconds)

        recency = tmp_graph.graph["ep-c"]["ep-d"]["recency_score"]
        assert recency == pytest.approx(0.990, abs=0.002)

    def test_time_delta_stored_in_seconds(self, tmp_graph):
        """原始 time_delta（秒）應保留在邊數據中供外部參考"""
        ep_a = _make_episode_mock("ep-e", "2026-01-01T00:00:00+00:00")
        ep_b = _make_episode_mock("ep-f", "2026-01-01T01:00:00+00:00")
        tmp_graph.add_episode_node(ep_a)
        tmp_graph.add_episode_node(ep_b)

        tmp_graph.add_precedes_edge("ep-e", "ep-f", time_delta=3600)

        edge_data = tmp_graph.graph["ep-e"]["ep-f"]
        assert edge_data["time_delta"] == 3600, "原始秒數應保留在 time_delta 欄位"
        assert "time_delta_hours" in edge_data, "應新增 time_delta_hours 欄位"
        assert edge_data["time_delta_hours"] == pytest.approx(1.0, abs=0.001)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Dream 更新路徑：update_edge_weights 以 edge_age（小時）計算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestUpdateEdgeWeightsTimeConversion:
    """確認 pruning.update_edge_weights 用 edge_age（小時）而非原始秒數"""

    def test_fresh_edge_keeps_high_recency_after_update(self, tmp_graph):
        """
        剛建立的邊（edge_age ≈ 0）執行 update_edge_weights 後，recency 應仍接近 1.0
        """
        ep_a = _make_episode_mock("ep-g", "2026-01-01T00:00:00+00:00")
        ep_b = _make_episode_mock("ep-h", "2026-01-01T00:01:00+00:00")
        tmp_graph.add_episode_node(ep_a)
        tmp_graph.add_episode_node(ep_b)
        tmp_graph.add_precedes_edge("ep-g", "ep-h", time_delta=60)

        pruning = GraphPruning(tmp_graph)
        pruning.update_edge_weights()

        recency_after = tmp_graph.graph["ep-g"]["ep-h"]["recency_score"]
        # 邊剛建立，edge_age 接近 0 小時，recency 應非常接近 1.0
        assert recency_after > 0.99, (
            f"剛建立的邊 update 後 recency={recency_after:.4f}，應 > 0.99"
        )

    def test_old_edge_has_lower_recency(self, tmp_graph):
        """
        模擬一條舊邊（created_at 設為 24 小時前），update 後 recency ≈ 0.787
        """
        ep_a = _make_episode_mock("ep-i", "2026-01-01T00:00:00+00:00")
        ep_b = _make_episode_mock("ep-j", "2026-01-01T01:00:00+00:00")
        tmp_graph.add_episode_node(ep_a)
        tmp_graph.add_episode_node(ep_b)
        tmp_graph.add_precedes_edge("ep-i", "ep-j", time_delta=3600)

        # 手動把 created_at 設為 24 小時前
        seconds_24h_ago = time.time() - 24 * 3600
        tmp_graph.graph["ep-i"]["ep-j"]["created_at"] = seconds_24h_ago

        pruning = GraphPruning(tmp_graph)
        pruning.update_edge_weights()

        recency_after = tmp_graph.graph["ep-i"]["ep-j"]["recency_score"]
        assert recency_after == pytest.approx(0.787, abs=0.005), (
            f"24 小時舊邊的 recency={recency_after:.4f}，應 ≈ 0.787"
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Config 讀取驗證
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestConfigIntegration:
    """確認 MemoryGraph 從 config 讀取 decay_lambda 而非硬編碼"""

    def test_decay_lambda_matches_config(self, tmp_graph):
        from echo_memory.config import get_config
        cfg = get_config()
        assert tmp_graph.decay_lambda == cfg.edge_recency_lambda, (
            f"graph.decay_lambda={tmp_graph.decay_lambda} 與 "
            f"config.edge_recency_lambda={cfg.edge_recency_lambda} 不一致"
        )

    def test_alpha_beta_gamma_match_config(self, tmp_graph):
        from echo_memory.config import get_config
        cfg = get_config()
        assert tmp_graph.alpha == cfg.edge_weight_alpha
        assert tmp_graph.beta == cfg.edge_weight_beta
        assert tmp_graph.gamma == cfg.edge_weight_gamma


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 換算常數驗證
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_seconds_per_hour_constant():
    """確認 _SECONDS_PER_HOUR 常數正確"""
    assert _SECONDS_PER_HOUR == 3600.0
