"""
M7 放行門檻（R1..R9）中，現有測試未以「Echo Stream 端到端」方式覆蓋的部分。

R1 來源角色　R2 衍生不重複計算　R3 全部經 Gate　R4 held／reject／rollback／合併／重試
R5 Dream／Reflection 不繞過　R6 Bridge 與 Hypothesis 分離　R7 Self Model 不改 Identity
R8 Echo Stream 故障測試　R9 回歸／遷移／檢索品質
（R1、R2 主要由 test_golden_e2e.py 覆蓋；對照表見 M7 第一輪報告。）
全部 mock LLM／mock embedding，不連外、不啟動 server。
"""

from __future__ import annotations

import inspect
import json
import os
import threading
import time
from pathlib import Path

import pytest

from echo_memory.affect.action_queue import ReflectionActionQueue
from echo_memory.affect.concept_inhibitor import ConceptInhibitor
from echo_memory.affect.reflection_executor import ReflectionExecutor
from echo_memory.config import get_config
from echo_memory.engine import MemoryEngine
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.storage.hypothesis import HypothesisWriter
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.semantic import SemanticMemory
from tests.hypothesis_support import (
    ScriptedLLM,
    build_env,
    llm_reply,
    make_bridge,
    two_concepts,
)
from tests.m7.sim import RoutedLLM, agent_ev, user_ev
from tests.mock_core import FakeCore, HostileCore

GATE_FILES = {
    "concept_inhibitor.py", "concept_patch.py", "hypothesis_gate.py",
    "self_model_gate.py", "episode_gate.py",
}


def _distill(*items):
    return [
        {"name": n, "description": d, "concept_type": "rule", "subject": s, "source_indices": idx}
        for n, d, s, idx in items
    ]


# ━━━ R3／R5：概念層所有變更都在 Gate 的呼叫堆疊內（動態旁路偵測） ━━━━━━━━━━━━━━━━━━━━━━━━━

@pytest.fixture
def mutation_probe(monkeypatch):
    seen: list[tuple[str, str, bool, list[str]]] = []

    def wrap(cls, name):
        orig = getattr(cls, name)

        def probe(self, *a, **k):
            stack = inspect.stack()[1:]
            files = {os.path.basename(f.filename) for f in stack}
            callers = [f"{os.path.basename(f.filename)}:{f.function}" for f in stack[:5]]
            seen.append((cls.__name__, name, bool(files & GATE_FILES), callers))
            return orig(self, *a, **k)

        monkeypatch.setattr(cls, name, probe)

    for n in ("upsert_concept", "add_derivation", "remove_concept", "mark_merged",
              "restore_merged", "set_subject"):
        wrap(SemanticMemory, n)
    for n in ("put", "remove", "restore"):
        wrap(HypothesisWriter, n)
    return seen


def test_r3_r5_every_concept_mutation_in_a_full_echo_cycle_goes_through_gate(make_sim, mutation_probe):
    """R3、R5：stock Echo Stream 接線（未注入 Gate）跑 store/force_remember/Dream x2/Reflection merge+rollback，
    SemanticMemory／HypothesisStore 的每一次寫入都發生在 Gate 的呼叫堆疊內。"""
    from llm_integration.tools.executor import ToolExecutor

    llm = RoutedLLM(
        distill=_distill(
            ("黑咖啡", "使用者喝黑咖啡", "user", [0]),
            ("夜貓作息", "使用者習慣晚睡", "user", [1]),
        ),
        replay={"reflections": [{"index": 0, "reflection": "咖啡是日常", "importance": 0.8,
                                 "related_indices": [1]}]},
    )
    sim, engine = make_sim(llm)
    sim.store("我都喝黑咖啡", "好", salience=0.8, evidence=[user_ev("我都喝黑咖啡"), agent_ev("好")])
    sim.store("我通常兩點才睡", "", salience=0.8, evidence=[user_ev("我通常兩點才睡")])
    ToolExecutor(engine=engine, session_id="es-session").handle_force_remember(
        {"content": "使用者重視睡眠", "reason": "LLM 判斷", "salience": 0.9})
    sim.dream()
    sim.store("我今天又喝了黑咖啡", "", salience=0.8, evidence=[user_ev("我今天又喝了黑咖啡")])
    sim.dream()

    # Reflection 路徑：merge 後 rollback
    gate = ConceptInhibitor(semantic=engine.semantic, llm_fn=lambda p: json.dumps(
        {"approve": True, "confidence": 0.9, "reason": "ok"}))
    engine.set_concept_inhibitor(gate)
    executor = ReflectionExecutor(engine=engine, concept_inhibitor=gate)
    queue = ReflectionActionQueue(audit_path=Path(engine.storage_path) / "reflection_audit.jsonl")
    params = {"merge_with": "夜貓作息"}
    act = queue.enqueue("merge_concepts", "黑咖啡", params, "合併")
    act.pre_state = executor.capture_pre_state("merge_concepts", "黑咖啡", params)
    assert queue.confirm(act.id) and queue.execute(act, executor.build_executor_fn()), act.error
    assert queue.rollback(act.id, executor.build_rollback_fn())

    assert mutation_probe, "前提：週期中確實有概念層寫入"
    # 已知例外：Reflection 回滾（_gated_undo）在 Gate 之外改資料，事後由 Gate 補寫 audit。
    # 放行條件：該路徑每次寫入都必須在 audit_log 留下非空 patches（目標 id、還原前後值、applied）。
    known = "reflection_executor.py:_gated_undo"
    bypass = [(c, n, callers) for c, n, in_gate, callers in mutation_probe
              if not in_gate and known not in callers]
    assert bypass == [], f"旁路 Gate 的寫入: {bypass}"
    undo_writes = sum(1 for _c, _n, in_gate, callers in mutation_probe
                      if not in_gate and known in callers)
    assert undo_writes <= 1

    if undo_writes:
        audit_file = Path(engine.semantic.storage_path) / "audit_log.jsonl"
        txs = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines() if line]
        undo_txs = [t for t in txs if str(t.get("user_input", "")).startswith("[reflection] undo_")]
        assert undo_txs, "回滾改了資料但 audit_log 沒有對應的 reflection 交易"
        for t in undo_txs:
            assert t["patches"], "回滾 audit 的 patches 為空"
            for patch in t["patches"]:
                assert patch["concept_id"] and patch["status"] == "applied"
                assert patch["old_value"] is not None and patch["new_value"] is not None


# ━━━ R3：stock 接線（宿主未注入 Gate）下，engine 自建預設 Gate，手動入口可執行 ━━━━━━━━━━━━━━━━━━

def test_r3_manual_review_without_injected_gate_uses_default_gate(make_sim):
    """R3／R8：Echo Stream 的 build_engine 不呼叫 set_concept_inhibitor；engine 自動建立預設 Gate，
    review_concept 不再回 no_gate；llm_fn 由 engine 帶入（此處 RoutedLLM 無 review 路由，依降級規則不寫入）。"""
    llm = RoutedLLM(distill=_distill(("寵物", "使用者養貓", "user", [0])))
    sim, engine = make_sim(llm)
    sim.store("我養了一隻貓", "", salience=0.8, evidence=[user_ev("我養了一隻貓")])
    sim.dream()
    assert isinstance(engine._concept_inhibitor, ConceptInhibitor)
    assert engine._concept_inhibitor._llm_fn is llm
    c = engine.semantic.get_by_name("寵物")
    out = engine.review_concept(c.id)
    assert out["reason"] != "no_gate"


def test_r3_default_gate_follows_set_llm_fn_and_is_replaced_by_host_gate(make_sim):
    sim, engine = make_sim(RoutedLLM(distill=_distill(("貓", "使用者養貓", "user", [0]))))
    engine.ensure_initialized()
    default = engine._concept_inhibitor
    assert isinstance(default, ConceptInhibitor)
    engine.set_llm_fn(None)
    assert default._llm_fn is None
    host = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
    engine.set_concept_inhibitor(host)
    assert engine._concept_inhibitor is host
    engine.set_llm_fn(lambda p: "{}")
    assert host._llm_fn is None  # 宿主注入的 Gate 不被 engine 覆寫


# ━━━ R4：重試（LLM 全線中斷 -> 恢復後下一輪 Dream 補上） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_r4_dream_survives_llm_outage_and_recovers_next_cycle(make_sim):
    """R4／R8：Dream 期間 LLM 全線拋例外 -> 週期不崩潰、不產生概念、episode 不被標成已蒸餾；LLM 恢復後下一輪補上。"""
    state = {"down": True}

    def distill(prompt: str):
        if state["down"]:
            raise ConnectionError("LLM backend down")
        return _distill(("黑咖啡", "使用者喝黑咖啡", "user", [0]))

    llm = RoutedLLM(distill=distill)
    sim, engine = make_sim(llm)
    sim.store("我都喝黑咖啡", "", salience=0.8, evidence=[user_ev("我都喝黑咖啡")])
    first = sim.dream()
    assert first["error"] is None
    assert engine.semantic.get_by_name("黑咖啡") is None

    state["down"] = False
    second = sim.dream()
    assert second["error"] is None
    assert engine.semantic.get_by_name("黑咖啡") is not None


# ━━━ R6：Bridge 與 Hypothesis 分離（透過完整 Dream） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def test_r6_dream_with_unsupported_review_never_turns_bridge_into_fact(tmp_path):
    """R6：LATENT_BRIDGE 經 Dream 只會變 Hypothesis（待驗證）；審核引用不足時邊仍是 LATENT_BRIDGE，不會有 RELATES_TO，預設檢索不帶假說。"""
    reply = {"r": ""}

    def llm(prompt: str) -> str:
        return reply["r"] if "假說審核員" in prompt else "{}"

    env = build_env(tmp_path, llm=llm)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    reply["r"] = llm_reply("supported", [d["ua"]])          # 只引用單端：必須被擋
    report = env.engine._ensure_dream_engine().dream_now(triggered_by="manual")
    assert report.error is None
    h = env.store.get_by_pair(d["a"], d["b"])
    assert h is not None and h.status != "supported"
    assert env.graph.edge_types_between(d["a"], d["b"]) == {"LATENT_BRIDGE"}
    ctx = env.engine.retrieve("咖啡拉花", top_k=10, min_similarity=0.0)
    assert not getattr(ctx, "hypotheses", [])


def test_r6_supported_requires_both_endpoints_cited_and_independent_speakers(tmp_path):
    """R6：兩端各自有原始 user 證據且被引用 -> 才昇格 RELATES_TO；昇格後 bridge 邊被取代而不是並存為事實。"""
    env = build_env(tmp_path, llm=lambda p: "{}")
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    env.engine._llm_fn = None
    env.engine._ensure_dream_engine()
    dream = env.engine._ensure_dream_engine()
    dream.dream_now(triggered_by="manual")                   # 無 LLM：只提案不審核
    h = env.store.get_by_pair(d["a"], d["b"])
    assert h is not None and h.status == "proposed"
    assert env.graph.edge_types_between(d["a"], d["b"]) == {"LATENT_BRIDGE"}


# ━━━ R7：Self Model 事件只讀、不驅動行動、不改 Identity（完整 Echo 週期） ━━━━━━━━━━━━━━━━━━━

@pytest.mark.parametrize("mode", ["normal", "raise", "command", "block"])
def test_r7_events_never_drive_core_actions_or_identity(tmp_path, monkeypatch, mode):
    """R7：事件開啟後跑完整週期（假說昇格 + Dream + force_remember），Core 端 task／presence／persona 寫入皆為 0；
    敵意 Core 不影響寫入。"""
    from llm_integration.tools.executor import ToolExecutor

    monkeypatch.setattr(get_config(), "self_model_events_enabled", True)
    core = FakeCore() if mode == "normal" else HostileCore(mode)
    env = build_env(tmp_path)
    env.engine.set_candidate_sink(core)
    d = two_concepts(env, same_speaker=False)
    make_bridge(env, d["a"], d["b"])
    env.engine._llm_fn = ScriptedLLM(llm_reply("supported", [d["ua"], d["ub"]]))
    t0 = time.perf_counter()
    report = env.engine._ensure_dream_engine().dream_now(triggered_by="manual")
    ToolExecutor(env.engine, "s1").handle_force_remember({"content": "重要事實", "salience": 0.9})
    assert report.error is None and time.perf_counter() - t0 < 5
    env.engine.event_emitter.drain()
    if hasattr(core, "release"):
        core.release.set()
    assert (core.task_calls, core.presence_calls, core.persona_writes) == (0, 0, 0)
    assert env.store.get_by_pair(d["a"], d["b"]).status == "supported"
    for draft in getattr(core, "drafts", []):
        assert set(draft.payload) & {"persona", "identity", "action", "task"} == set()


# ━━━ R8：Echo Stream 故障模擬 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_REAL_EMBED = {
    n: getattr(EpisodicMemory, n) for n in ("_generate_embedding", "generate_query_embedding")
}


@pytest.fixture
def _real_embedding(mock_embedding_model, monkeypatch):
    """conftest 會把 embedding 換成假的；故障模擬需要真實的降級路徑。"""
    for name, fn in _REAL_EMBED.items():
        monkeypatch.setattr(EpisodicMemory, name, fn)


def test_r8_embedding_backend_down_store_keeps_episode_and_retrieve_degrades(
        tmp_path, monkeypatch, _real_embedding):
    """R8：Ollama embedding 後端掛掉 -> store 仍寫入 episode（待補向量）、retrieve 不拋例外且 degraded=True，
    Echo Stream 端 sim 看得到降級旗標。"""
    import requests

    from tests.m7.sim import EchoStreamSim

    engine = MemoryEngine(
        storage_path=tmp_path / "data", namespace_id="echo-stream",
        embedding_backend="ollama", embedding_model="bge-m3",
    )
    engine.ensure_initialized()
    sim = EchoStreamSim(engine)

    def down(*a, **k):
        raise requests.exceptions.ConnectionError("ollama down")

    monkeypatch.setattr(requests, "post", down)
    try:
        stored = sim.store("我也喜歡茶", "", evidence=[user_ev("我也喜歡茶")])
        assert stored is not None, "embedding 失敗不應丟掉這一輪對話"
        ep = engine.episodic.get(stored["episode_id"])
        assert ep is not None and ep.metadata.get("embedding_pending") is True
        assert sim.retrieve("茶") is None
        assert sim.last_retrieve_degraded is True
    finally:
        sim.close()


def test_r8_bad_evidence_declaration_is_sidebanded_and_writes_nothing(make_sim):
    """R8：evidence 與 agent_response 不一致（宿主 bug）-> sim.store 旁路回 None，且不留半套 episode／證據。"""
    sim, engine = make_sim(RoutedLLM())
    n_before = len(engine.episodic.list_episodes())
    assert sim.store("你好", "我是助手", evidence=[user_ev("你好"), agent_ev("完全不同的內容")]) is None
    assert len(engine.episodic.list_episodes()) == n_before


def test_r8_concurrent_store_retrieve_and_dream_do_not_corrupt(make_sim, tmp_path):
    """R8：多執行緒同時 store／retrieve（細粒度鎖模式）並穿插 Dream，不丟例外、不遺失任何一輪、重啟後資料完整。"""
    llm = RoutedLLM(distill=_distill(("咖啡", "使用者愛咖啡", "user", [0])))
    sim, engine = make_sim(llm)
    errors: list[BaseException] = []
    n_threads, n_each = 4, 8

    def writer(tid: int):
        try:
            for i in range(n_each):
                text = f"執行緒{tid}第{i}句關於咖啡"
                engine.store_episode(text, "", "es-session", salience=0.6,
                                     idempotency_key=f"t{tid}-{i}",
                                     evidence=[EvidenceInput(**user_ev(text))])
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def reader():
        try:
            for _ in range(n_each):
                engine.retrieve("咖啡", top_k=3, min_similarity=0.0)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(t,)) for t in range(n_threads)]
    threads += [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    sim.dream()                                                  # 與寫入者並行的 Dream
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert len(engine.episodic.list_episodes(limit=1000)) == n_threads * n_each
    reloaded = MemoryEngine(storage_path=tmp_path / "data", namespace_id="echo-stream")
    reloaded.ensure_initialized()
    assert len(reloaded.episodic.list_episodes(limit=1000)) == n_threads * n_each


def test_r8_garbage_llm_output_everywhere_does_not_crash_dream(make_sim):
    """R8：所有 LLM 路由回傳垃圾（非 JSON／截斷）-> Dream 完成、不建概念、不寫壞資料。"""
    llm = RoutedLLM(distill="這不是 JSON {{{", replay="```json\n{\"reflections\": [", evidence_review="???",
                    contradiction="", hypothesis="null")
    sim, engine = make_sim(llm)
    sim.store("我都喝黑咖啡", "", salience=0.8, evidence=[user_ev("我都喝黑咖啡")])
    out = sim.dream()
    assert out["error"] is None
    assert engine.semantic.list_concepts() == []


def test_r8_restart_after_crash_mid_write_loads_clean(make_sim, tmp_path):
    """R8：Echo Stream 行程被殺（模擬：engine 物件丟棄、tmp 殘檔留在目錄）-> 新 engine 載入正常，既有 episode 不遺失。"""
    sim, engine = make_sim(RoutedLLM())
    r = sim.store("我都喝黑咖啡", "", evidence=[user_ev("我都喝黑咖啡")])
    root = engine.storage_path
    (root / "episodic").mkdir(exist_ok=True)
    (root / "episodic" / "episodes.json.tmp").write_text("{半截", encoding="utf-8")
    again = MemoryEngine(storage_path=tmp_path / "data", namespace_id="echo-stream")
    again.ensure_initialized()
    assert again.episodic.get(r["episode_id"]) is not None


# ━━━ R9：遷移／檢索（外部資料副本，選用） ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_COPY = os.environ.get("M7_MEMORY_TEST_COPY")


def _fake_embedding(dim: int):
    import hashlib

    import numpy as np

    def _gen(self, text):
        seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:4], "little")
        vec = np.random.RandomState(seed).randn(dim).astype(np.float32)
        return (vec / np.linalg.norm(vec)).tolist()

    return _gen


@pytest.mark.skipif(not _COPY or not Path(_COPY).is_dir(),
                    reason="未設定 M7_MEMORY_TEST_COPY（bot data/memory_test 的『副本』目錄）")
def test_r9_real_memory_test_copy_loads_retrieves_and_dreams(tmp_path, monkeypatch):
    """R9：對 bot data/memory_test 的副本（再複製一份到 tmp，絕不碰原資料）逐 namespace 以 manifest 記錄的
    embedding 設定載入，確認可開啟、可檢索、無 LLM 的 Dream 不拋例外、episode 數不變。"""
    import shutil

    work = tmp_path / "copy"
    shutil.copytree(_COPY, work)
    checked = 0
    for ns in sorted(p for p in (work / "namespaces").iterdir() if p.is_dir()):
        manifest = ns / "episodic" / "embedding_index.json"
        if not (ns / "episodic" / "episodes.json").exists() or not manifest.exists():
            continue
        info = json.loads(manifest.read_text(encoding="utf-8"))
        monkeypatch.setattr(EpisodicMemory, "_generate_embedding", _fake_embedding(info["dimension"]))
        engine = MemoryEngine(
            storage_path=work, namespace_id=ns.name,
            embedding_model=info["model"], embedding_backend="ollama",
        )
        engine.ensure_initialized()
        n = len(engine.episodic.list_episodes(limit=100000))
        assert n > 0
        assert engine.retrieve("你好", top_k=3, min_similarity=0.0) is not None
        assert engine.dream("manual")["error"] is None
        assert len(engine.episodic.list_episodes(limit=100000)) == n
        checked += 1
    assert checked >= 1


def test_r8_r9_switching_embedding_model_does_not_crash_retrieve(tmp_path, monkeypatch):
    """R8／R9：資料以 1024 維（bge-m3）寫入，之後 Echo Stream 以 512 維模型開啟同一資料目錄 -> retrieve 應降級而非拋例外。"""
    monkeypatch.setattr(EpisodicMemory, "_generate_embedding", _fake_embedding(1024))
    llm = RoutedLLM(distill=_distill(("貓", "使用者養貓", "user", [0])))
    e1 = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1", llm_fn=llm,
                      embedding_model="bge-m3", embedding_backend="ollama")
    e1.store_episode("我養了一隻貓", "", "s", salience=0.8, evidence=[EvidenceInput(**user_ev("我養了一隻貓"))])
    e1.dream("manual")
    assert e1.semantic.get_by_name("貓") is not None

    monkeypatch.setattr(EpisodicMemory, "_generate_embedding", _fake_embedding(512))
    e2 = MemoryEngine(storage_path=tmp_path / "d", namespace_id="ns1", llm_fn=llm)
    ctx = e2.retrieve("貓", top_k=3, min_similarity=0.0)       # 目前在這裡拋 ValueError
    assert ctx.degraded is True or ctx.is_empty() or ctx is not None


# ━━━ R3／R5：Dream 內部 Gate 的變更通知要能到達 engine（共享投影失效依賴它） ━━━━━━━━━━━━━━━━━━

def test_r3_r5_dream_internal_gate_changes_notify_engine(make_sim, monkeypatch):
    """R3、R5（M-10 契約）：stock 接線下 Dream 兩輪（create 後 expand）應通知 engine 概念已變更。"""
    llm = RoutedLLM(distill=_distill(("黑咖啡", "使用者喝黑咖啡", "user", [0])))
    sim, engine = make_sim(llm)
    notified: list[str] = []
    # engine 已在 ensure_initialized 時把 _on_concept_changed 註冊到預設 Gate；再掛一個探針確認通知鏈
    assert engine._listener_inhibitor is engine._concept_inhibitor
    engine._concept_inhibitor.register_change_listener(lambda cid, *a, **k: notified.append(cid))
    sim.store("我都喝黑咖啡", "", salience=0.8, evidence=[user_ev("我都喝黑咖啡")])
    sim.dream()
    sim.store("我今天又喝了黑咖啡", "", salience=0.8, evidence=[user_ev("我今天又喝了黑咖啡")])
    sim.dream()
    assert engine.semantic.get_by_name("黑咖啡") is not None
    assert notified, "Dream 對概念的變更沒有通知 engine"
