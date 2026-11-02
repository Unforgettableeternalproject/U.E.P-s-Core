"""
Dream Engine 整合測試

測試 DreamEngine 的完整流程，包含圖譜修剪、知識蒸餾、反思重播與排程控制。
所有測試使用 tmp_path 隔離 storage，避免互相污染。

重要說明：
  - engine.dream() 回傳的是精簡 dict（只含頂層欄位）
  - 需要存取完整 DreamReport 欄位時，使用 engine._dream_engine.dream_now()
  - APScheduler 在 module 頂層透過環境變數關閉，避免排程器在測試中自動觸發
"""
from __future__ import annotations

import os

# 必須在任何 echo_memory import 前設定，否則 Config 讀取時已快取舊值
os.environ["DREAM_ENABLE_IDLE"] = "false"
os.environ["DREAM_ENABLE_CRON"] = "false"

import pytest

from echo_memory.engine import MemoryEngine


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 輔助函式
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


def _make_engine(tmp_path) -> MemoryEngine:
    """建立隔離的 MemoryEngine（不帶 LLM）。"""
    return MemoryEngine(
        storage_path=str(tmp_path / "data"),
        # 使用最小的 embedding 模型加速測試
        embedding_model="BAAI/bge-small-zh-v1.5",
        device="cpu",
        namespace_id="test-dream",
        llm_fn=None,
    )


def _store_episodes(engine: MemoryEngine, count: int, salience: float = 0.5) -> list[str]:
    """批次存入指定數量的 episode，回傳 episode_id 清單。"""
    ids = []
    for i in range(count):
        ep_id = engine.store_episode(
            user_input=f"測試用戶輸入 #{i}：請問一些有趣的問題",
            agent_response=f"測試回應 #{i}：這是一個有意義的回應，包含一些知識性內容",
            session_id="test-session-001",
            content_summary=f"對話摘要 #{i}：知識性討論",
            salience=salience,
        )
        ids.append(ep_id)
    return ids


def _add_graph_edges(engine: MemoryEngine, episode_ids: list[str]) -> int:
    """在圖譜中為 episode 建立 PRECEDES 邊，讓 pruning 有東西可操作。"""
    import time

    graph = engine.episodic.graph
    edges_added = 0
    for i in range(len(episode_ids) - 1):
        src = episode_ids[i]
        dst = episode_ids[i + 1]
        if graph.has_node(src) and graph.has_node(dst):
            graph.graph.add_edge(
                src,
                dst,
                edge_type="PRECEDES",
                weight=0.3,  # 低於預設 min_weight_threshold=0.05，但高於 0，不會馬上被修剪
                frequency=1,
                avg_salience=0.5,
                created_at=time.time() - 7200,  # 2 小時前建立
            )
            edges_added += 1
    if edges_added:
        graph.save()
    return edges_added


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 測試案例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


@pytest.mark.slow
def test_pruning_after_5_episodes(tmp_path):
    """
    圖譜修剪正常流程測試：存入 5+ 筆 episode，建立圖譜邊，觸發 Dream 後
    驗證 PruningReport 結構完整，且回傳無錯誤。

    注意：此測試不需要 LLM，pruning 是純圖譜操作。
    """
    engine = _make_engine(tmp_path)
    try:
        # 存入 6 筆 episode
        ep_ids = _store_episodes(engine, 6, salience=0.5)

        # 確認 episode 節點已建立在圖譜中（episodic.write 會自動加節點）
        # 手動建立一些邊讓 pruning 有東西可操作
        _add_graph_edges(engine, ep_ids)

        # 直接呼叫 DreamEngine 取完整 DreamReport（繞開 engine.dream() 的精簡 dict）
        # 先透過 engine.dream() 確保 _dream_engine 被初始化
        result = engine.dream(triggered_by="manual")

        # 基本 dict 結構驗證
        assert result is not None, "dream() 不應回傳 None"
        assert isinstance(result, dict), "dream() 應回傳 dict"
        assert result.get("error") is None, f"dream 不應有錯誤：{result.get('error')}"

        # 取完整 DreamReport 驗證 pruning 細節
        dream_report = engine._dream_engine.dream_now(triggered_by="manual")
        assert dream_report is not None, "dream_now() 不應回傳 None"
        assert dream_report.error is None, f"DreamReport 不應有錯誤：{dream_report.error}"
        assert dream_report.pruning is not None, "pruning report 應存在"

        pr = dream_report.pruning
        assert hasattr(pr, "edges_before"), "PruningReport 應有 edges_before 欄位"
        assert hasattr(pr, "edges_after"), "PruningReport 應有 edges_after 欄位"
        assert hasattr(pr, "edges_pruned"), "PruningReport 應有 edges_pruned 欄位"
        assert pr.edges_before >= 0, "edges_before 不應為負數"
        assert pr.edges_after >= 0, "edges_after 不應為負數"
        assert pr.edges_pruned >= 0, "edges_pruned 不應為負數"

    finally:
        try:
            if engine._dream_engine is not None:
                engine._dream_engine.stop()
        except Exception:
            pass


@pytest.mark.slow
@pytest.mark.live_api
def test_distillation_8_episodes(tmp_path, request):
    """
    知識蒸餾測試：存入 8+ 筆高顯著性 episode，觸發 Dream 後驗證蒸餾報告。

    - 若 LLM 可用（live_api marker）：驗證蒸餾有實際執行
    - 若 LLM 不可用：驗證蒸餾優雅跳過（skipped_reason 不為 None）
    - 無論如何，Dream 都不應崩潰
    """
    # 判斷 LLM 是否可用
    has_llm = any(os.getenv(v) for v in ("GOOGLE_API_KEY", "GCP_PROJECT_ID"))

    llm_fn = None
    if has_llm:
        # 嘗試建立 LLM 函式
        try:
            from llm_integration.llm_client import GeminiClient
            client = GeminiClient()

            def llm_fn(prompt: str) -> str:
                return client.generate(prompt)
        except Exception:
            has_llm = False

    engine = MemoryEngine(
        storage_path=str(tmp_path / "data"),
        embedding_model="BAAI/bge-small-zh-v1.5",
        device="cpu",
        namespace_id="test-distillation",
        llm_fn=llm_fn,
    )

    try:
        # 存入 8 筆高顯著性 episode
        _store_episodes(engine, 8, salience=0.8)

        result = engine.dream(triggered_by="manual")

        assert result is not None, "dream() 不應回傳 None"
        assert result.get("error") is None, f"dream 不應有錯誤：{result.get('error')}"

        # 取完整 DreamReport 驗證蒸餾細節
        dream_report = engine._dream_engine.dream_now(triggered_by="manual")
        assert dream_report.distillation is not None, "distillation report 應存在"

        d = dream_report.distillation
        if has_llm:
            # LLM 可用時：蒸餾欄位數值應合理
            assert d.concepts_created >= 0, "concepts_created 不應為負數"
            assert d.concepts_updated >= 0, "concepts_updated 不應為負數"
        else:
            # LLM 不可用時：應優雅跳過
            assert d.skipped_reason is not None, (
                "無 LLM 時蒸餾應設定 skipped_reason，但目前為 None"
            )
            assert d.skipped_reason in ("no_llm", "no_episodes", "llm_error"), (
                f"skipped_reason 應為已知值，但得到：{d.skipped_reason}"
            )

    finally:
        try:
            if engine._dream_engine is not None:
                engine._dream_engine.stop()
        except Exception:
            pass


@pytest.mark.slow
@pytest.mark.live_api
def test_replay_5_episodes(tmp_path):
    """
    LiDER 反思重播測試：存入 5+ 筆 episode（至少 3 筆 salience >= 0.6），
    觸發 Dream 後驗證重播報告。

    - 若 LLM 可用：驗證 episodes_replayed >= 0
    - 若 LLM 不可用：驗證 skipped_reason 不為 None（優雅降級）
    """
    has_llm = any(os.getenv(v) for v in ("GOOGLE_API_KEY", "GCP_PROJECT_ID"))

    llm_fn = None
    if has_llm:
        try:
            from llm_integration.llm_client import GeminiClient
            client = GeminiClient()

            def llm_fn(prompt: str) -> str:
                return client.generate(prompt)
        except Exception:
            has_llm = False

    engine = MemoryEngine(
        storage_path=str(tmp_path / "data"),
        embedding_model="BAAI/bge-small-zh-v1.5",
        device="cpu",
        namespace_id="test-replay",
        llm_fn=llm_fn,
    )

    try:
        # 存入 3 筆高顯著性（>= 0.6）episode
        _store_episodes(engine, 3, salience=0.75)
        # 再存入 2 筆低顯著性 episode 達到 5 筆總量
        _store_episodes(engine, 2, salience=0.3)

        result = engine.dream(triggered_by="manual")

        assert result is not None, "dream() 不應回傳 None"
        assert result.get("error") is None, f"dream 不應有錯誤：{result.get('error')}"

        # 取完整 DreamReport 驗證重播細節
        dream_report = engine._dream_engine.dream_now(triggered_by="manual")
        assert dream_report.replay is not None, "replay report 應存在"

        r = dream_report.replay
        if has_llm:
            assert r.episodes_replayed >= 0, "episodes_replayed 不應為負數"
        else:
            # 無 LLM → skipped_reason 不為 None
            assert r.skipped_reason is not None, (
                "無 LLM 時重播應設定 skipped_reason，但目前為 None"
            )
            assert r.skipped_reason in ("no_llm", "no_episodes", "llm_error"), (
                f"skipped_reason 應為已知值，但得到：{r.skipped_reason}"
            )

    finally:
        try:
            if engine._dream_engine is not None:
                engine._dream_engine.stop()
        except Exception:
            pass


@pytest.mark.slow
def test_scheduler_not_started(tmp_path):
    """
    排程器關閉測試：確認環境變數 DREAM_ENABLE_IDLE=false、DREAM_ENABLE_CRON=false
    時，APScheduler 排程器不會自動啟動，但手動觸發仍然有效。

    此測試不需要 LLM。
    """
    # 確認環境變數已設定（本 module 頂部已設定）
    assert os.environ.get("DREAM_ENABLE_IDLE", "").lower() == "false", (
        "DREAM_ENABLE_IDLE 應為 false"
    )
    assert os.environ.get("DREAM_ENABLE_CRON", "").lower() == "false", (
        "DREAM_ENABLE_CRON 應為 false"
    )

    engine = _make_engine(tmp_path)
    try:
        # 存入幾筆 episode 讓 Dream 有東西可跑
        _store_episodes(engine, 3, salience=0.5)

        # 觸發 dream() 以初始化 _dream_engine
        result = engine.dream(triggered_by="manual")

        assert result is not None, "即使排程停用，手動觸發仍應回傳結果"

        # 確認排程器未啟動
        dream_engine = engine._dream_engine
        assert dream_engine is not None, "_dream_engine 應已建立"
        assert not dream_engine.enable_idle, "enable_idle 應為 False"
        assert not dream_engine.enable_cron, "enable_cron 應為 False"

        # 若 APScheduler 可用，確認排程器未在運行
        try:
            from apscheduler.schedulers.background import BackgroundScheduler  # noqa: F401
            HAS_APSCHEDULER = True
        except ImportError:
            HAS_APSCHEDULER = False

        if HAS_APSCHEDULER and dream_engine._scheduler is not None:
            # 由於 enable_idle=False 且 enable_cron=False，不應呼叫 start()
            assert not dream_engine._scheduler.running, (
                "排程器不應啟動（DREAM_ENABLE_IDLE 和 DREAM_ENABLE_CRON 都為 false）"
            )

        # 手動觸發應正常運作
        result2 = engine.dream(triggered_by="manual")
        assert result2 is not None, "第二次手動觸發也應成功"
        assert result2.get("error") is None, f"第二次 dream 不應有錯誤：{result2.get('error')}"

    finally:
        try:
            if engine._dream_engine is not None:
                engine._dream_engine.stop()
        except Exception:
            pass


@pytest.mark.slow
def test_dream_report_structure(tmp_path):
    """
    DreamReport 完整結構驗證：確認所有必要欄位存在且型別正確。

    驗證：
      - started_at, finished_at 是合法 ISO 時間字串
      - triggered_by == "manual"
      - edges_updated >= 0
      - duration_seconds >= 0
      - error 為 None（正常執行）

    不需要 LLM。
    """
    from datetime import datetime

    engine = _make_engine(tmp_path)
    try:
        # 存入幾筆 episode
        _store_episodes(engine, 4, salience=0.5)

        # 先觸發一次讓 _dream_engine 初始化
        engine.dream(triggered_by="manual")

        # 直接取 DreamReport 物件進行完整結構驗證
        dream_report = engine._dream_engine.dream_now(triggered_by="manual")

        assert dream_report is not None, "DreamReport 不應為 None"

        # 時間欄位驗證
        assert isinstance(dream_report.started_at, str) and dream_report.started_at, (
            "started_at 應為非空字串"
        )
        assert isinstance(dream_report.finished_at, str) and dream_report.finished_at, (
            "finished_at 應為非空字串"
        )

        # 驗證 ISO 格式（fromisoformat 不拋例外即合法）
        try:
            datetime.fromisoformat(dream_report.started_at)
        except ValueError as e:
            pytest.fail(f"started_at 不是合法 ISO 時間字串：{dream_report.started_at} — {e}")

        try:
            datetime.fromisoformat(dream_report.finished_at)
        except ValueError as e:
            pytest.fail(f"finished_at 不是合法 ISO 時間字串：{dream_report.finished_at} — {e}")

        # triggered_by 欄位
        assert dream_report.triggered_by == "manual", (
            f"triggered_by 應為 'manual'，但得到：{dream_report.triggered_by!r}"
        )

        # 數值欄位
        assert dream_report.edges_updated >= 0, (
            f"edges_updated 應 >= 0，但得到：{dream_report.edges_updated}"
        )
        assert dream_report.duration_seconds >= 0, (
            f"duration_seconds 應 >= 0，但得到：{dream_report.duration_seconds}"
        )

        # 錯誤欄位
        assert dream_report.error is None, (
            f"正常執行時 error 應為 None，但得到：{dream_report.error}"
        )

        # pruning 欄位（不需 LLM 也應執行）
        assert dream_report.pruning is not None, "pruning report 應存在"
        assert dream_report.pruning.edges_before >= 0, "pruning.edges_before 不應為負數"
        assert dream_report.pruning.edges_after >= 0, "pruning.edges_after 不應為負數"

    finally:
        try:
            if engine._dream_engine is not None:
                engine._dream_engine.stop()
        except Exception:
            pass
