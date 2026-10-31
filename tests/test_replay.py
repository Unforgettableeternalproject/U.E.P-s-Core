"""
LiDER Full Replay 測試 — Phase 2 Branch 5 驗收

驗收標準：
  1. dream() 後 is_dreamed 被更新
  2. 邊權重有反思調整（強化或新建）
  3. 無 LLM 降級跳過
  4. Episode 顯著性被反思結果調整
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from echo_memory.dream.replay import LiDERReplay, ReplayReport
from echo_memory.schemas.episode import Episode
from echo_memory.storage.episodic import EpisodicMemory

MOCK_DIM = 512


def _mock_llm_replay(prompt: str) -> str:
    """模擬 LLM 反思輸出。"""
    return json.dumps({
        "reflections": [
            {
                "index": 0,
                "reflection": "使用者對壽司有強烈偏好",
                "importance": 0.8,
                "related_indices": [1],
            },
            {
                "index": 1,
                "reflection": "週末計畫與飲食偏好相關",
                "importance": 0.6,
                "related_indices": [0],
            },
            {
                "index": 2,
                "reflection": "天氣話題較瑣碎",
                "importance": 0.2,
                "related_indices": [],
            },
        ]
    }, ensure_ascii=False)


def _mock_llm_error(prompt: str) -> str:
    raise RuntimeError("LLM unavailable")


@pytest.fixture
def storage(tmp_path: Path):
    ep_dir = tmp_path / "episodic"
    ep_dir.mkdir()
    episodic = EpisodicMemory(storage_path=ep_dir)
    graph = episodic.graph
    return episodic, graph


def _store_episodes(episodic: EpisodicMemory) -> list[Episode]:
    texts = [
        ("我最喜歡吃壽司", "壽司是很棒的料理！"),
        ("週末想去日本料理店", "推薦新開的那家。"),
        ("今天天氣真好", "適合出門走走。"),
    ]
    episodes = []
    for u, a in texts:
        ep = Episode(
            user_input=u, agent_response=a,
            session_id="test", content_summary=u,
            salience_score=0.7,
        )
        episodic.write(ep)
        episodes.append(ep)
    return episodes


class TestReplayCore:
    """核心重播功能。"""

    def test_replay_records_episode_ids(self, storage):
        """Replay 記錄被重播的 Episode ID（不再自己標 is_dreamed）。"""
        episodic, graph = storage
        episodes = _store_episodes(episodic)

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        report = replayer.run()

        assert report.skipped_reason is None
        assert report.episodes_replayed == 3
        # 記錄了被重播的 ID（供 DreamEngine 統一標記）
        assert len(report._replayed_episode_ids) == 3
        # 但 Replay 自己不標記 is_dreamed
        for ep in episodes:
            loaded = episodic.get(ep.id)
            assert loaded.is_dreamed is False

    def test_replay_adjusts_salience(self, storage):
        episodic, graph = storage
        episodes = _store_episodes(episodic)
        [ep.salience_score for ep in episodes]

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        replayer.run()

        # 反思後顯著性應該被調整（平均原始值和 importance）
        for i, ep in enumerate(episodes):
            loaded = episodic.get(ep.id)
            # 不一定都變，但至少有些會變
            # Episode 0: (0.7+0.8)/2 = 0.75, Episode 2: (0.7+0.2)/2 = 0.45
            assert loaded is not None

        # 確認高 importance 的保持較高
        ep0 = episodic.get(episodes[0].id)
        ep2 = episodic.get(episodes[2].id)
        assert ep0.salience_score > ep2.salience_score

    def test_replay_creates_or_strengthens_edges(self, storage):
        episodic, graph = storage
        _store_episodes(episodic)

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        report = replayer.run()

        # LLM 說 [0]和[1] 相關 → 應該有邊被強化或新建
        total_edge_changes = report.edges_strengthened + report.edges_created
        assert total_edge_changes > 0

    def test_replay_skips_already_dreamed(self, storage):
        """手動標記 is_dreamed 後，Replay 跳過。"""
        episodic, graph = storage
        episodes = _store_episodes(episodic)

        # 手動標記（模擬 DreamEngine 統一標記）
        episodic.mark_as_dreamed([ep.id for ep in episodes])

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        report = replayer.run()
        assert report.skipped_reason == "no_episodes"

    def test_replay_report_avg_importance(self, storage):
        episodic, graph = storage
        _store_episodes(episodic)

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        report = replayer.run()

        # (0.8 + 0.6 + 0.2) / 3 ≈ 0.533
        assert report.avg_importance == pytest.approx(0.533, abs=0.01)


class TestReplayDegradation:
    """優雅降級。"""

    def test_no_llm_skips(self, storage):
        episodic, graph = storage
        _store_episodes(episodic)

        replayer = LiDERReplay(episodic, graph, llm_fn=None)
        report = replayer.run()
        assert report.skipped_reason == "no_llm"

    def test_llm_error_handled(self, storage):
        episodic, graph = storage
        _store_episodes(episodic)

        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_error)
        report = replayer.run()
        assert report.skipped_reason == "llm_error"

    def test_no_episodes_skips(self, storage):
        episodic, graph = storage
        replayer = LiDERReplay(episodic, graph, llm_fn=_mock_llm_replay)
        report = replayer.run()
        assert report.skipped_reason == "no_episodes"


class TestReplayReport:
    def test_summary_format(self):
        report = ReplayReport(
            episodes_replayed=3,
            edges_strengthened=1,
            edges_created=2,
            avg_importance=0.6,
        )
        s = report.summary()
        assert "3" in s
        assert "0.60" in s


class TestCollectCandidatesWindow:
    """候選須先濾未 dream 再截斷，已 dreamed 高 salience 不得佔滿視窗。"""

    def test_low_salience_pending_not_starved_by_dreamed(self, storage):
        from datetime import datetime, timedelta

        episodic, _ = storage
        for i in range(210):
            episodic.write(Episode(
                user_input=f"done{i}", agent_response="r", session_id="t",
                content_summary=f"done{i}", salience_score=0.9, is_dreamed=True,
            ))
        base = datetime(2026, 1, 1)
        pending = []
        for i, sal in enumerate([0.45, 0.41, 0.462]):
            ep = Episode(
                user_input=f"p{i}", agent_response="r", session_id="t",
                content_summary=f"p{i}", salience_score=sal,
                dream_scanned_at=base + timedelta(days=i),
            )
            episodic.write(ep)
            pending.append(ep)
        low = Episode(
            user_input="low", agent_response="r", session_id="t",
            content_summary="low", salience_score=0.2,
        )
        episodic.write(low)

        replay = LiDERReplay(episodic=episodic, graph=episodic.graph, llm_fn=_mock_llm_replay)
        got = replay._collect_candidates(0.4, 10)

        assert {e.id for e in got} == {e.id for e in pending}
        # 排序規則不變：dream_scanned_at 最舊先
        assert [e.id for e in got] == [e.id for e in pending]
        assert all(not e.is_dreamed and e.salience_score >= 0.4 for e in got)

    def test_never_scanned_first_keeps_salience_desc(self, storage):
        episodic, _ = storage
        for sal in (0.41, 0.8, 0.5):
            episodic.write(Episode(
                user_input=str(sal), agent_response="r", session_id="t",
                content_summary=str(sal), salience_score=sal,
            ))
        replay = LiDERReplay(episodic=episodic, graph=episodic.graph, llm_fn=_mock_llm_replay)
        got = replay._collect_candidates(0.4, 2)
        assert [e.salience_score for e in got] == [0.8, 0.5]
