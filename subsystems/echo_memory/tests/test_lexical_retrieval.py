"""M-6b：Episode lexical（BM25）檢索路 + RRF 融合。

測試中的向量一律是 conftest 的雜湊假向量（互相近乎正交），因此向量路對任何
查詢都幾乎不會過門檻——專有名詞要命中只能靠 lexical 路。不載入真實模型。
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from echo_memory import MemoryEngine
from echo_memory.config import get_config
from echo_memory.retrieval.ecphory import EcphoryRetrieval
from echo_memory.schemas.episode import Episode
from echo_memory.schemas.evidence import EvidenceInput
from echo_memory.storage.episodic import EpisodicMemory
from echo_memory.storage.lexical import LexicalIndex, build_docs, tokenize
from echo_memory.storage.semantic import SemanticMemory


@pytest.fixture(autouse=True)
def _lexical_on(monkeypatch):
    # 預設關閉；本檔測試明確開啟。假向量近乎正交，故關掉 lexical-only 的 cosine 低標
    cfg = get_config()
    monkeypatch.setattr(cfg, "retrieval_lexical_enabled", True)
    monkeypatch.setattr(cfg, "retrieval_lexical_min_cosine", -1.0)


@pytest.fixture
def stores(tmp_path):
    epi = EpisodicMemory(storage_path=tmp_path / "ep", embedding_model="bge-small")
    sem = SemanticMemory(storage_path=tmp_path / "sem")
    return epi, sem


def _ep(user, agent="", summary=None, session="s1"):
    return Episode(
        user_input=user,
        agent_response=agent,
        session_id=session,
        content_summary=summary if summary is not None else f"{user} {agent}".strip(),
    )


def _fill(epi, n=12):
    for i in range(n):
        epi.write(_ep(f"今天聊到第{i}件日常小事，天氣和午餐", "好的我記下了"))


# ─── 切詞 / BM25 ──────────────────────────────────────────────────


def test_tokenize_cjk_bigram_and_alnum():
    toks = tokenize("艾爾登法環 Elden-Ring 2024")
    assert {"艾", "爾", "艾爾", "爾登", "法環"} <= set(toks)
    assert {"elden", "ring", "2024"} <= set(toks)
    assert tokenize("") == []


def test_bm25_ranks_rare_term_first():
    docs = [("a", "今天吃了拉麵"), ("b", "我在玩艾爾登法環"), ("c", "今天天氣很好")]
    hits = LexicalIndex().query(docs, "艾爾登法環", top_n=5)
    assert [d for d, _ in hits] == ["b"]


def test_single_char_matches_alone_do_not_qualify():
    docs = [("a", "我愛吃蘋果"), ("b", "他去了公園")]
    # 查詢與文件只共用單字（弱詞）時不應命中：需至少一個 bigram／詞
    assert LexicalIndex().query(docs, "你吃飯嗎我們走", top_n=5) == []


# ─── 專有名詞命中（向量給低分）────────────────────────────────────


def test_proper_noun_found_by_lexical_when_vector_misses(stores):
    epi, sem = stores
    _fill(epi)
    target = _ep("我最近沉迷艾爾登法環", "那款遊戲很難")
    epi.write(target)

    ctx = EcphoryRetrieval(epi, sem).retrieve("艾爾登法環", min_similarity=0.35)

    assert target.id in [e.id for e in ctx.episodes]
    assert target.id in ctx.lexical_only_ids
    # similarities 語意仍是原始 cosine（假向量近乎正交，不會是 lexical 分數）
    assert abs(ctx.similarities[target.id]) < 0.35
    assert "lexical" in ctx.timings_ms
    assert not ctx.degraded


def test_flag_off_behaves_like_legacy(stores, monkeypatch):
    epi, sem = stores
    _fill(epi)
    target = _ep("我最近沉迷艾爾登法環", "那款遊戲很難")
    epi.write(target)
    monkeypatch.setattr(get_config(), "retrieval_lexical_enabled", False)

    ctx = EcphoryRetrieval(epi, sem).retrieve("艾爾登法環", min_similarity=0.35)

    assert target.id not in [e.id for e in ctx.episodes]
    assert ctx.lexical_only_ids == []
    assert "lexical" not in ctx.timings_ms
    assert "seed_search" in ctx.timings_ms


def test_no_lexical_hits_matches_flag_off(stores, monkeypatch):
    """查詢沒有任何 lexical 命中時，開啟旗標的結果與關閉旗標一致。"""
    epi, sem = stores
    _fill(epi)
    query = "完全無關zzqq"

    def run():
        ctx = EcphoryRetrieval(epi, sem).retrieve(query, min_similarity=-1.0, top_k=6)
        return [e.id for e in ctx.episodes], dict(ctx.similarities)

    on = run()
    monkeypatch.setattr(get_config(), "retrieval_lexical_enabled", False)
    assert run() == on


# ─── RRF ─────────────────────────────────────────────────────────


def test_rrf_order_and_cosine_semantics(stores):
    epi, sem = stores
    ret = EcphoryRetrieval(epi, sem)
    a, b, c = _ep("a"), _ep("b"), _ep("c")
    for e in (a, b, c):
        epi.write(e)
    vector = [(a, 0.9), (b, 0.5)]
    lex = [(b, 7.0), (c, 3.0)]
    q = epi.generate_query_embedding("c")

    seeds, lexical_only, rank_sim = ret._rrf_seeds(vector, lex, 3, 60, q, 0.35)

    # b 兩路都有 -> 第一；其後 a（向量第 1）先於 c（lexical 第 2）
    assert [e.id for e, _ in seeds] == [b.id, a.id, c.id]
    assert dict((e.id, s) for e, s in seeds)[a.id] == 0.9  # 向量路保持原始 cosine
    assert lexical_only == [c.id]
    assert rank_sim[c.id] >= 0.35  # 排序用相似度不低於向量門檻
    assert b.id not in rank_sim


# ─── 降級 ────────────────────────────────────────────────────────


def test_embedding_degraded_still_returns_lexical_results(stores, monkeypatch):
    epi, sem = stores
    _fill(epi)
    target = _ep("我的角色代號是霧島零式", "收到")
    epi.write(target)
    monkeypatch.setattr(epi, "try_generate_query_embedding", lambda q: [])
    epi.last_embedding_error = "連線逾時"

    ctx = EcphoryRetrieval(epi, sem).retrieve("霧島零式")

    assert ctx.degraded is True
    assert "僅 lexical" in ctx.degraded_reason and "連線逾時" in ctx.degraded_reason
    assert [e.id for e in ctx.episodes] == [target.id]
    assert ctx.similarities[target.id] == 0.0
    assert ctx.lexical_only_ids == [target.id]


def test_embedding_degraded_without_hits_is_empty(stores, monkeypatch):
    epi, sem = stores
    _fill(epi)
    monkeypatch.setattr(epi, "try_generate_query_embedding", lambda q: [])
    epi.last_embedding_error = "down"

    ctx = EcphoryRetrieval(epi, sem).retrieve("查無此詞zzqq")

    assert ctx.degraded is True and ctx.degraded_reason == "down"
    assert ctx.is_empty()


def test_embedding_degraded_flag_off_is_empty(stores, monkeypatch):
    epi, sem = stores
    epi.write(_ep("我的角色代號是霧島零式", "收到"))
    monkeypatch.setattr(epi, "try_generate_query_embedding", lambda q: [])
    epi.last_embedding_error = "down"
    monkeypatch.setattr(get_config(), "retrieval_lexical_enabled", False)

    ctx = EcphoryRetrieval(epi, sem).retrieve("霧島零式")

    assert ctx.degraded and ctx.is_empty() and ctx.degraded_reason == "down"


# ─── 未交付 agent 內容 ───────────────────────────────────────────


def _ev(user, reply, state="delivered", units=None):
    return [
        EvidenceInput(
            source_type="user", content=user, speaker_id="u",
            occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        EvidenceInput(
            source_type="agent", content=reply, speaker_id="agent",
            delivery_state=state, delivery_unit="sentence", delivered_units=units,
        ),
    ]


def test_undelivered_agent_content_is_not_lexically_matched(tmp_path):
    engine = MemoryEngine(storage_path=tmp_path / "data", namespace_id="ns1")
    # 1) not_delivered：agent_response 為空，但呼叫端自訂 content_summary 夾帶預定回覆
    nd = engine.store_episode(
        "今天好累", "", "s1",
        content_summary="今天好累 預定回覆：幻影代號白鴉",
        evidence=_ev("今天好累", "幻影代號白鴉", state="not_delivered"),
    )
    # 2) 被打斷且無進度：agent_response 保留完整預定回覆
    pn = engine.store_episode(
        "我養了貓", "貓很可愛。暗號蒼狼之牙", "s1",
        evidence=_ev("我養了貓", "貓很可愛。暗號蒼狼之牙", state="partial", units=None),
    )
    # 3) 已交付：正常可命中
    ok = engine.store_episode(
        "我喜歡咖啡", "手沖咖啡暗號紫電", "s1",
        evidence=_ev("我喜歡咖啡", "手沖咖啡暗號紫電"),
    )

    def ids(q):
        return {e.id for e in engine.retrieve(q).episodes}

    assert nd not in ids("幻影代號白鴉")
    assert pn not in ids("蒼狼之牙")
    assert ok in ids("暗號紫電")
    # 使用者那半句仍可命中未交付的 episode（那是 user 說過的）
    assert nd in ids("今天好累")


# ─── 並行 ────────────────────────────────────────────────────────


def test_concurrent_lexical_search_and_write(stores):
    epi, _ = stores
    _fill(epi, 6)
    errors: list[BaseException] = []
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                epi.lexical_search("日常小事 天氣", top_n=5)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    def writer():
        try:
            for i in range(15):
                epi.write(_ep(f"並行寫入第{i}筆含關鍵詞蒼穹", "ok"))
            epi.delete_episode(epi.list_episodes()[0].id)
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    readers = [threading.Thread(target=reader) for _ in range(3)]
    for t in readers:
        t.start()
    w = threading.Thread(target=writer)
    w.start()
    w.join(timeout=60)
    stop.set()
    for t in readers:
        t.join(timeout=10)

    assert not errors, errors
    assert epi.lexical_search("蒼穹", top_n=50)


def test_index_follows_mutations(stores):
    epi, _ = stores
    e = _ep("原始內容普通", "")
    epi.write(e)
    assert epi.lexical_search("獨特詞彙") == []
    epi.update_summary(e.id, "改成獨特詞彙")
    assert [x.id for x, _ in epi.lexical_search("獨特詞彙")] == [e.id]
    epi.delete_episode(e.id)
    assert epi.lexical_search("獨特詞彙") == []


def test_build_docs_skips_duplicate_summary():
    e = _ep("你好嗎", "我很好")
    ((_, text),) = build_docs([e])
    assert text.count("你好嗎") == 1


# ─── 雜訊抑制（統計方式，無關鍵字清單）────────────────────────────


def _noisy_corpus(n=40):
    docs = [(f"d{i}", f"今天第{i}號什麼都沒做只是閒聊天氣") for i in range(n)]
    docs.append(("job", "我的工作是軟體工程師，在台北上班"))
    docs.append(("only_q", "他問我說什麼"))
    return docs


def test_high_df_question_words_do_not_match():
    idx = LexicalIndex()
    docs = _noisy_corpus()
    # 「什麼」出現在多數文件：df 過高被剔除，查詢只剩疑問詞時不命中任何文件
    assert idx.query(docs, "什麼", top_n=10, max_df=0.2, min_coverage=0.5) == []
    hits = idx.query(docs, "我的工作是什麼", top_n=10, max_df=0.2, min_coverage=0.5)
    ids = [d for d, _ in hits]
    assert ids and ids[0] == "job"
    assert "only_q" not in ids and not any(i.startswith("d") for i in ids)


def test_coverage_requires_enough_query_terms():
    docs = [("a", "艾爾登法環是一款遊戲"), ("b", "法環是一個圓形的環")] + [
        (f"f{i}", f"第{i}則無關的日常紀錄內容") for i in range(30)
    ]
    idx = LexicalIndex()
    loose = {d for d, _ in idx.query(docs, "艾爾登法環", top_n=10, min_coverage=0.0)}
    strict = {d for d, _ in idx.query(docs, "艾爾登法環", top_n=10, min_coverage=0.75)}
    assert "b" in loose and "a" in loose
    assert strict == {"a"}


def test_lexical_only_needs_min_cosine(stores, monkeypatch):
    epi, sem = stores
    _fill(epi)
    target = _ep("我最近沉迷艾爾登法環", "那款遊戲很難")
    epi.write(target)
    # 假向量 cosine 約 0；低標 0.25 時字面巧合不得成為種子
    monkeypatch.setattr(get_config(), "retrieval_lexical_min_cosine", 0.25)
    ctx = EcphoryRetrieval(epi, sem).retrieve("艾爾登法環", min_similarity=0.35)
    assert target.id not in [e.id for e in ctx.episodes]


def test_lexical_default_is_off(monkeypatch):
    from echo_memory.config import Config
    monkeypatch.delenv("RETRIEVAL_LEXICAL_ENABLED", raising=False)
    assert Config().retrieval_lexical_enabled is False


def test_prompt_marks_lexical_only_hits(stores):
    from echo_memory.schemas.memory_context import MemoryContext
    a, b = _ep("向量命中"), _ep("字面命中的內容")
    ctx = MemoryContext(
        episodes=[a, b], similarities={a.id: 0.8, b.id: 0.3}, lexical_only_ids=[b.id],
    )
    text = ctx.to_text()
    assert "字面命中" in text and "30%" not in text and "80%" in text
