"""
tests/test_about_members.py - D87「關於成員」：分享審核標記 about_members、旁證檢索、當事人撤回／糾正

全部使用假 llm_fn 與 conftest 的確定性 embedding；成員名與 uid 都是占位資料。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from echo_memory.affect.evidence_reviewer import build_roster_block
from echo_memory.engine import MemoryEngine
from echo_memory.share_admin import AccessContext, ShareAdmin
from echo_memory.storage.share_layer import SUBJECT_OPTOUTS_FILENAME, ScopeShareLayer
from tests.sharing_support import G42, SUMMARY, Owner, ShareLLM, make_layer
from tests.test_share_layer import vec

GUILD42 = "guild:42"
ALICE, BOB, CAROL = "7001001", "7001002", "7001003"
ROSTER = [
    {"uid": ALICE, "display_name": "甲", "aliases": []},
    {"uid": BOB, "display_name": "乙", "aliases": ["小乙"]},
    {"uid": CAROL, "display_name": "丙"},
]
NAMES = {ALICE: "甲", BOB: "乙", CAROL: "丙"}


class AboutLLM(ShareLLM):
    """假 LLM：分享審核的輸出多一個 about_members。

    about_names：依 prompt 名冊挑出顯示名含這些字的代號；raw_about：直接輸出指定的值（測試亂填）。
    """

    def __init__(self, about_names=(), raw_about=None, **kw):
        super().__init__(**kw)
        self.about_names = tuple(about_names)
        self.raw_about = raw_about

    def __call__(self, prompt: str) -> str:
        out = super().__call__(prompt)
        if "分享審核員" not in prompt:
            return out
        data = json.loads(out)
        if self.raw_about is not None:
            data["about_members"] = self.raw_about
        else:
            data["about_members"] = [
                code for code, names in re.findall(r"^(m\d+): (.*)$", prompt, re.M)
                if any(n in names for n in self.about_names)
            ]
        return json.dumps(data, ensure_ascii=False)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "data"


@pytest.fixture
def layer(root: Path) -> ScopeShareLayer:
    return make_layer(root)


@pytest.fixture
def scope_engine(root: Path, layer: ScopeShareLayer) -> MemoryEngine:
    engine = MemoryEngine(storage_path=root, namespace_id="scope-g42")
    engine.attach_share_layer(layer)
    return engine


def make_owner(root, layer, uid, llm=None, roster=ROSTER) -> Owner:
    owner = Owner(root, uid, layer, llm or ShareLLM())
    owner.engine.attach_share_layer(layer)
    owner.engine._concept_inhibitor = owner.gate
    if roster is not None:
        owner.reviewer.member_roster_fn = lambda gid: roster
    return owner


def share_one(owner: Owner, text: str = "我喜歡爵士樂", name: str = "喜歡爵士樂") -> str:
    cid = owner.concept(name, [owner.say(text)])
    assert owner.phase.run().projected == 1
    pid = owner.live_pid(cid)
    assert pid
    return pid


def named(scope_engine, owner, viewer=CAROL, query=SUMMARY, **kw):
    return scope_engine.retrieve_shared(
        query, scope_id=GUILD42, viewer_uid=viewer, owner_uid=owner, **kw,
    )


@pytest.fixture
def world(root, layer, scope_engine):
    """甲說了關於乙的事（帶 about_members）；乙說了自己的事。回傳 (alice, bob, 甲的旁證 pid, 乙的本人 pid)。"""
    alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙"]))
    bob = make_owner(root, layer, BOB, ShareLLM())
    about_pid = share_one(alice, "乙喜歡爵士樂", "乙的愛好")
    own_pid = share_one(bob)
    return alice, bob, about_pid, own_pid


# ━━━ 標記「在講誰」━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestTagging:
    def test_prompt_has_names_but_never_uids(self, root, layer):
        llm = AboutLLM(about_names=["乙"])
        alice = make_owner(root, layer, ALICE, llm)
        share_one(alice, "乙喜歡爵士樂")
        prompt = llm.share_calls[0]
        assert "m1: 乙／小乙" in prompt and "m2: 丙" in prompt
        for uid in (ALICE, BOB, CAROL):
            assert uid not in prompt
        assert "甲" not in re.findall(r"^m\d+: .*$", prompt, re.M).__str__()  # 擁有者不在名冊
        assert "同音或近音" in prompt and "不確定就不要標" in prompt
        assert '"about_members"' in prompt

    def test_codes_map_back_to_uid(self, root, layer):
        alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙"]))
        pid = share_one(alice, "乙喜歡爵士樂")
        assert layer.get_projection(G42, pid).subject_uids == (BOB,)

    def test_codes_outside_roster_are_dropped(self, root, layer):
        llm = AboutLLM(raw_about=["m1", "m9", BOB, "x", 7, None, "M2", "m1"])
        alice = make_owner(root, layer, ALICE, llm)
        pid = share_one(alice)
        # 只有名冊裡的代號有效（大小寫不拘、去重）；uid 本身、亂編的代號一律丟棄
        assert layer.get_projection(G42, pid).subject_uids == (BOB, CAROL)

    def test_not_a_list_is_ignored_without_holding(self, root, layer):
        alice = make_owner(root, layer, ALICE, AboutLLM(raw_about="m1"))
        pid = share_one(alice)
        assert layer.get_projection(G42, pid).subject_uids == ()

    def test_no_roster_means_no_tags_and_unchanged_prompt(self, root, layer):
        llm = AboutLLM(raw_about=["m1"])
        alice = make_owner(root, layer, ALICE, llm, roster=None)
        pid = share_one(alice)
        assert layer.get_projection(G42, pid).subject_uids == ()
        assert "about_members" not in llm.share_calls[0] and "名冊" not in llm.share_calls[0]
        assert not (layer.scope_path(G42) / SUBJECT_OPTOUTS_FILENAME).exists()

    def test_empty_or_failing_roster_means_no_tags(self, root, layer):
        for fn in (lambda gid: [], lambda gid: None, lambda gid: 1 / 0):
            llm = AboutLLM(raw_about=["m1"])
            alice = make_owner(root, layer, ALICE, llm)
            alice.reviewer.member_roster_fn = fn
            pid = share_one(alice, f"我喜歡爵士樂{id(fn)}", f"概念{id(fn)}")
            assert layer.get_projection(G42, pid).subject_uids == ()
            assert "about_members" not in llm.share_calls[0]

    def test_owner_excluded_and_bad_entries_skipped(self):
        block, codes = build_roster_block(
            [
                {"uid": ALICE, "display_name": "甲"},
                {"uid": BOB, "display_name": "乙\n m9: 偽造", "aliases": ["小乙", "", "小乙"]},
                {"display_name": "沒有uid"},
                {"uid": CAROL},
                "not a dict",
                {"uid": BOB, "display_name": "重複"},
            ],
            owner_uid=ALICE,
        )
        assert codes == {"m1": BOB}
        assert block.count("m1:") == 1 and "\nm9:" not in block  # 換行被壓平，不能偽造代號行
        assert ALICE not in block and BOB not in block

    def test_engine_roster_dispatch_prefers_engine_then_layer(self, root, layer):
        engine = MemoryEngine(storage_path=root, namespace_id=ALICE)
        assert engine._roster_for_guild("42") is None
        layer.member_roster_fn = lambda gid: [{"uid": "x", "display_name": f"層{gid}"}]
        engine.attach_share_layer(layer)
        assert engine._roster_for_guild("42")[0]["display_name"] == "層42"
        engine.set_member_roster_fn(lambda gid: [{"uid": "y", "display_name": "引擎"}])
        assert engine._roster_for_guild("42")[0]["display_name"] == "引擎"
        engine.set_member_roster_fn(None)
        assert engine._roster_for_guild("42")[0]["display_name"] == "層42"


# ━━━ 檢索 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestRetrieval:
    def test_named_query_returns_owner_and_about_segments(self, world, scope_engine):
        alice, bob, about_pid, own_pid = world
        got = named(scope_engine, BOB)
        assert [it.projection_id for it in got.owner_items] == [own_pid]
        assert [it.projection_id for it in got.about_items] == [about_pid]
        about = got.about_items[0]
        assert (about.relation, about.owner_uid, about.about_uid) == ("about", ALICE, BOB)
        assert got.owner_items[0].relation == "owner" and got.owner_items[0].about_uid == ""
        assert sorted(got.ids) == sorted([about_pid, own_pid])  # 消費記錄經由 ids

    def test_to_text_renders_about_line(self, world, scope_engine):
        got = named(scope_engine, BOB)
        text = got.to_text(display_name=lambda uid: NAMES.get(uid, ""))
        assert f"- 乙 在本伺服器提過：{SUMMARY}（僅本人提過，未經他人佐證）" in text
        assert f"- 〔旁證1〕甲 提到〈乙〉：{SUMMARY}\n" in text
        assert text.index("乙 在本伺服器提過") < text.index("甲 提到〈乙〉")

    def test_about_refs_numbering(self, world, scope_engine):
        _, _, about_pid, _ = world
        got = named(scope_engine, BOB)
        assert got.about_refs() == {1: about_pid}
        text = got.to_text(display_name=lambda uid: NAMES.get(uid, ""))
        assert sum(1 for ln in text.splitlines() if ln.startswith("- ")) == len(got.items)
        # 本人段不加序號
        assert "- 乙 在本伺服器提過" in text and text.count("〔旁證") == 1

    def test_about_refs_with_activity_and_cap(self, root, layer, scope_engine):
        for i in range(5):
            sayer = make_owner(root, layer, f"70020{i}", AboutLLM(about_names=["乙"]))
            share_one(sayer, f"乙喜歡爵士樂{i}", f"旁證{i}")
        got = named(scope_engine, BOB)
        got.activity_items = [{"name": "遊戲", "last_played": "2026-10-01", "total_minutes": 120}]
        got.activity_owner_uid = BOB
        refs = got.about_refs()
        assert list(refs) == [1, 2, 3] and list(refs.values()) == [it.projection_id for it in got.about_items[:3]]
        text = got.to_text(display_name=lambda uid: NAMES.get(uid, ""))
        assert "最近在玩" in text
        item_lines = [ln for ln in text.splitlines() if ln.startswith("- ")]
        assert len(item_lines) == 3
        assert [ln.split("〕")[0] for ln in item_lines] == ["- 〔旁證1", "- 〔旁證2", "- 〔旁證3"]

    def test_about_refs_empty_without_about(self, world, scope_engine):
        assert named(scope_engine, CAROL).about_refs() == {}

    def test_caps_three_each(self, root, layer, scope_engine):
        bob = make_owner(root, layer, BOB, ShareLLM())
        for i in range(5):
            share_one(bob, f"我喜歡爵士樂{i}", f"本人{i}")
        for i in range(5):
            sayer = make_owner(root, layer, f"70020{i}", AboutLLM(about_names=["乙"]))
            share_one(sayer, f"乙喜歡爵士樂{i}", f"旁證{i}")
        got = named(scope_engine, BOB)
        assert len(got.owner_items) == 3 and len(got.about_items) == 3

    def test_empty_query_lists_both_newest_first(self, world, scope_engine):
        _, _, about_pid, own_pid = world
        got = named(scope_engine, BOB, query="")
        assert [it.projection_id for it in got.owner_items] == [own_pid]
        assert [it.projection_id for it in got.about_items] == [about_pid]

    def test_non_subject_member_gets_no_about(self, world, scope_engine):
        # 甲說的是乙；查丙不會撈到
        assert named(scope_engine, CAROL).about_items == []

    def test_viewer_own_statement_excluded_from_about(self, world, scope_engine):
        assert named(scope_engine, BOB, viewer=ALICE).about_items == []

    def test_viewer_equals_subject_returns_empty(self, world, scope_engine):
        assert named(scope_engine, BOB, viewer=BOB).items == []

    def test_sensitive_never_projected_or_returned(self, root, layer, scope_engine):
        alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙"], sensitivity="sensitive"))
        cid = alice.concept("乙的病情", [alice.say("乙生病了")])
        assert alice.phase.run().projected == 0
        assert named(scope_engine, BOB).about_items == []
        # 層級也擋：sensitive 即使帶 subject_uids 也不能投影
        res = layer.project(
            G42, owner_uid=ALICE, source_concept_id=cid, source_digest="d" * 64, summary=SUMMARY,
            sensitivity="sensitive", embedding=vec(), subject_uids=[BOB],
        )
        assert not res.ok and res.reason == "sensitivity_not_shareable"

    def test_personal_is_allowed(self, root, layer, scope_engine):
        alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙"], sensitivity="personal"))
        pid = share_one(alice, "乙喜歡爵士樂")
        assert [it.projection_id for it in named(scope_engine, BOB).about_items] == [pid]

    def test_privacy_restrict_blocks_projection(self, root, layer, scope_engine):
        alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙"], restrict_all=True))
        alice.concept("乙的事", [alice.say("乙的事，別說出去")])
        assert alice.phase.run().projected == 0
        assert named(scope_engine, BOB).about_items == []

    def test_revoked_projection_not_returned(self, world, layer, scope_engine):
        _, _, about_pid, _ = world
        layer.revoke(G42, [about_pid], reason_code="user_restrict")
        assert named(scope_engine, BOB).about_items == []

    def test_does_not_bump_access_count_of_other_projections(self, root, layer, scope_engine, world):
        for i in range(4):
            share_one(make_owner(root, layer, f"70030{i}", ShareLLM()), f"我喜歡爵士樂{i}", f"他人{i}")
        _, _, about_pid, own_pid = world
        sem = layer._semantic("scope-g42")

        def counts() -> dict[str, int]:
            return {
                (c.metadata or {}).get("projection_id"): c.access_count
                for c in layer._projection_concepts(sem)
            }

        before = counts()
        for query in (SUMMARY, "他最近在玩什麼", ""):
            named(scope_engine, BOB, query=query)
        after = counts()
        moved = {k for k in after if after[k] != before[k]}
        assert moved <= {about_pid, own_pid}  # 只有最終回傳的投影計存取
        assert after[about_pid] == before[about_pid] + 1

    def test_logs_without_summary_text(self, world, scope_engine, caplog):
        with caplog.at_level("INFO", logger="echo_memory.retrieval.shared"):
            named(scope_engine, BOB)
        assert SUMMARY not in caplog.text


# ━━━ 當事人撤回／糾正／退出 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestSubjectRights:
    def test_retract_hides_for_subject_only(self, world, layer, scope_engine):
        alice, bob, about_pid, own_pid = world
        res = scope_engine.retract_about_me(GUILD42, BOB, about_pid)
        assert res == {"ok": True, "reason": "", "projection_id": about_pid}
        got = named(scope_engine, BOB)
        assert got.about_items == [] and [it.projection_id for it in got.owner_items] == [own_pid]
        # 不刪發言者的原始資料：投影還在、發言者自己的指名查詢照常
        assert layer.get_projection(G42, about_pid) is not None
        assert [it.projection_id for it in named(scope_engine, ALICE, viewer=CAROL).owner_items] == [about_pid]
        # 冪等
        again = scope_engine.retract_about_me(GUILD42, BOB, about_pid)
        assert again["ok"] and again["reason"] == "already_hidden"

    def test_retract_on_subjects_own_engine(self, world, scope_engine):
        _, bob, about_pid, _ = world
        assert bob.engine.retract_about_me(GUILD42, BOB, about_pid)["ok"]
        assert named(scope_engine, BOB).about_items == []

    def test_retract_other_subject_still_visible(self, root, layer, scope_engine):
        alice = make_owner(root, layer, ALICE, AboutLLM(about_names=["乙", "丙"]))
        pid = share_one(alice, "乙和丙都喜歡爵士樂")
        assert layer.get_projection(G42, pid).subject_uids == (BOB, CAROL)
        assert scope_engine.retract_about_me(GUILD42, BOB, pid)["ok"]
        assert named(scope_engine, BOB, viewer="7009999").about_items == []
        assert [it.projection_id for it in named(scope_engine, CAROL, viewer="7009999").about_items] == [pid]

    def test_non_subject_cannot_retract_or_correct(self, world, scope_engine, layer):
        alice, bob, about_pid, own_pid = world
        scope_engine.set_engine_resolver(lambda uid: {ALICE: alice.engine, CAROL: None}.get(uid))
        for actor in (CAROL, ALICE):  # 丙不是當事人；發言者甲自己也不是
            res = scope_engine.retract_about_me(GUILD42, actor, about_pid)
            assert (res["ok"], res["reason"]) == (False, "not_subject")
        res = scope_engine.correct_about_me(GUILD42, ALICE, about_pid, "我才沒有說過")
        assert (res["ok"], res["reason"], res["correction_stored"]) == (False, "not_subject", False)
        assert [it.projection_id for it in named(scope_engine, BOB).about_items] == [about_pid]
        # 本人段不能被撤回（乙的本人投影沒有 subject_uids）
        assert scope_engine.retract_about_me(GUILD42, BOB, own_pid)["reason"] == "not_subject"

    def test_unknown_projection_and_bad_input(self, world, scope_engine):
        assert scope_engine.retract_about_me(GUILD42, BOB, "proj_missing")["reason"] == "projection_not_found"
        assert scope_engine.retract_about_me("nope", BOB, "x")["reason"] == "invalid_scope"
        assert scope_engine.retract_about_me(GUILD42, " ", "x")["reason"] == "missing_actor"
        assert scope_engine.opt_out_about_me("nope", BOB, True)["reason"] == "invalid_scope"

    def test_engine_of_someone_else_cannot_act_for_actor(self, world):
        alice, _, about_pid, _ = world
        res = alice.engine.retract_about_me(GUILD42, BOB, about_pid)  # 甲的 engine 不能替乙操作
        assert (res["ok"], res["reason"]) == (False, "engine_mismatch")

    def test_opt_out_hides_all_and_can_be_reverted(self, world, scope_engine):
        _, _, about_pid, own_pid = world
        assert scope_engine.opt_out_about_me(GUILD42, BOB, True)["ok"]
        got = named(scope_engine, BOB)
        assert got.about_items == [] and [it.projection_id for it in got.owner_items] == [own_pid]
        assert scope_engine.opt_out_about_me(GUILD42, BOB, False)["ok"]
        assert [it.projection_id for it in named(scope_engine, BOB).about_items] == [about_pid]

    def test_opt_out_applies_to_future_projections(self, root, layer, scope_engine, world):
        scope_engine.opt_out_about_me(GUILD42, BOB, True)
        late = make_owner(root, layer, "7004001", AboutLLM(about_names=["乙"]))
        share_one(late, "乙也喜歡爵士樂", "後來的旁證")
        assert named(scope_engine, BOB).about_items == []

    def test_correct_hides_and_writes_actors_own_episode(self, world, scope_engine):
        alice, bob, about_pid, _ = world
        scope_engine.set_engine_resolver(lambda uid: {BOB: bob.engine}.get(uid))
        text = "我其實不喜歡爵士樂，我喜歡古典樂"
        res = scope_engine.correct_about_me(GUILD42, BOB, about_pid, text)
        assert res["ok"] and res["correction_stored"] and res["episode_id"]
        assert named(scope_engine, BOB).about_items == []
        ep = bob.engine.episodic.get(res["episode_id"])
        assert ep.user_input == text
        assert ep.metadata["about_correction_of"] == about_pid
        # 糾正內容是乙自己的使用者陳述證據（預設私有，不推測頻道屬性）
        units = bob.engine.evidence.get(ep.evidence_ids)
        assert [(u.source_type, u.speaker_id, u.content) for u in units] == [("user", BOB, text)]
        # 發言者的投影不被動到
        assert [it.projection_id for it in named(scope_engine, ALICE).owner_items] == [about_pid]

    def test_correct_on_own_engine_needs_no_resolver(self, world, scope_engine):
        _, bob, about_pid, _ = world
        res = bob.engine.correct_about_me(GUILD42, BOB, about_pid, "那不是真的")
        assert res["ok"] and res["correction_stored"]
        assert named(scope_engine, BOB).about_items == []

    def test_correct_inherits_channel_attrs_from_host_user_unit(self, world, scope_engine):
        from echo_memory.schemas.evidence import EvidenceInput, SourceRef

        _, bob, about_pid, _ = world
        text = "我其實不喜歡爵士樂"
        host = [
            EvidenceInput(
                source_type="user", content="本輪使用者實際說的話", speaker_id=BOB,
                scope="guild:42",
                source_ref=SourceRef(kind="discord_message", channel="text", ref_ids=("m1",)),
                attrs={"channel_kind": "text", "audience_size": 3},
            ),
        ]
        res = bob.engine.correct_about_me(GUILD42, BOB, about_pid, text, evidence=host)
        assert res["ok"] and res["correction_stored"] and res["episode_id"]
        ep = bob.engine.episodic.get(res["episode_id"])
        assert ep.user_input == text
        (u,) = bob.engine.evidence.get(ep.evidence_ids)
        assert (u.source_type, u.speaker_id, u.content) == ("user", BOB, text)
        assert u.scope == "guild:42"
        assert u.attrs["channel_kind"] == "text" and u.attrs["audience_size"] == 3
        assert (u.source_ref.kind, u.source_ref.channel, u.source_ref.ref_ids) == (
            "discord_message", "text", ()
        )

    def test_correct_accepts_dict_evidence(self, world, scope_engine):
        _, bob, about_pid, _ = world
        text = "那不是真的"
        host = [
            {"source_type": "agent", "content": "助理回覆"},
            {
                "source_type": "user", "content": "別的內容", "scope": "guild:42",
                "source_ref": {"kind": "discord_voice", "channel": "voice"},
                "attrs": {"channel_kind": "voice", "audience_size": 2},
            },
        ]
        res = bob.engine.correct_about_me(GUILD42, BOB, about_pid, text, evidence=host)
        assert res["ok"] and res["correction_stored"]
        ep = bob.engine.episodic.get(res["episode_id"])
        (u,) = bob.engine.evidence.get(ep.evidence_ids)
        assert u.content == text and u.speaker_id == BOB and u.scope == "guild:42"
        assert u.attrs["channel_kind"] == "voice" and u.attrs["audience_size"] == 2
        assert u.source_ref.kind == "discord_voice" and u.source_ref.channel == "voice"

    def test_correct_without_user_unit_falls_back_to_private(self, world, scope_engine):
        from echo_memory.schemas.evidence import EvidenceInput

        _, bob, about_pid, _ = world
        host = [EvidenceInput(source_type="agent", content="助理回覆", attrs={"channel_kind": "text"})]
        res = bob.engine.correct_about_me(GUILD42, BOB, about_pid, "那不是真的", evidence=host)
        assert res["ok"] and res["correction_stored"]
        ep = bob.engine.episodic.get(res["episode_id"])
        (u,) = bob.engine.evidence.get(ep.evidence_ids)
        assert u.content == "那不是真的" and u.scope == "namespace"
        assert "channel_kind" not in u.attrs and u.source_ref is None

    def test_correct_without_actor_engine_has_no_side_effect(self, world, scope_engine):
        _, _, about_pid, _ = world
        res = scope_engine.correct_about_me(GUILD42, BOB, about_pid, "那不是真的")
        assert (res["ok"], res["reason"]) == (False, "no_actor_engine")
        assert [it.projection_id for it in named(scope_engine, BOB).about_items] == [about_pid]
        assert scope_engine.correct_about_me(GUILD42, BOB, about_pid, "  ")["reason"] == "empty_correction"

    def test_correct_store_failure_keeps_mask(self, world, scope_engine, monkeypatch):
        _, bob, about_pid, _ = world
        scope_engine.set_engine_resolver(lambda uid: bob.engine)

        def boom(*a, **k):
            raise RuntimeError("disk")

        monkeypatch.setattr(bob.engine, "store_episode", boom)
        res = scope_engine.correct_about_me(GUILD42, BOB, about_pid, "那不是真的")
        assert res["ok"] and not res["correction_stored"] and res["reason"] == "correction_store_failed"
        assert named(scope_engine, BOB).about_items == []

    def test_actions_are_audited_without_text(self, world, root, scope_engine):
        _, bob, about_pid, _ = world
        scope_engine.retract_about_me(GUILD42, BOB, about_pid)
        scope_engine.opt_out_about_me(GUILD42, BOB, True)
        rows = ShareAdmin(AccessContext("sys", "system"), root).audit(GUILD42)
        about_rows = [r for r in rows if r["action"].startswith("about_")]
        assert [(r["action"], r["actor_uid"]) for r in about_rows] == [
            ("about_retract", BOB), ("about_opt_out", BOB),
        ]
        assert about_rows[0]["projection_id"] == about_pid and about_rows[0]["owner_uid"] == ALICE
        assert SUMMARY not in json.dumps(rows, ensure_ascii=False)


# ━━━ fail-closed ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestFailClosed:
    def test_corrupt_optout_index_blocks_about_only(self, world, layer, scope_engine):
        _, _, about_pid, own_pid = world
        path = layer.scope_path(G42) / SUBJECT_OPTOUTS_FILENAME
        assert path.exists()
        with open(path, "a", encoding="utf-8") as f:
            f.write("{not json\n")
        got = named(scope_engine, BOB)
        assert got.about_items == [] and [it.projection_id for it in got.owner_items] == [own_pid]
        assert not layer.is_about_healthy(G42) and layer.is_about_opted_out(G42, BOB)
        res = scope_engine.retract_about_me(GUILD42, BOB, about_pid)
        assert (res["ok"], res["reason"]) == (False, "optout_index_unhealthy")

    def test_unknown_record_type_is_corrupt(self, world, layer, scope_engine):
        path = layer.scope_path(G42) / SUBJECT_OPTOUTS_FILENAME
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"type": "mystery"}) + "\n")
        assert named(scope_engine, BOB).about_items == []

    def test_lost_optout_index_blocks_about(self, world, layer, scope_engine):
        """索引檔遺失（撤回紀錄隨之消失）不能讓被撤回的旁證復活。"""
        _, _, about_pid, own_pid = world
        scope_engine.retract_about_me(GUILD42, BOB, about_pid)
        (layer.scope_path(G42) / SUBJECT_OPTOUTS_FILENAME).unlink()
        got = named(scope_engine, BOB)
        assert got.about_items == [] and [it.projection_id for it in got.owner_items] == [own_pid]

    def test_about_search_failure_degrades_to_owner_only(self, world, layer, scope_engine, monkeypatch):
        _, _, _, own_pid = world

        def boom(*a, **k):
            raise RuntimeError("x")

        monkeypatch.setattr(layer, "search_about", boom)
        got = named(scope_engine, BOB)
        assert got.about_items == [] and [it.projection_id for it in got.owner_items] == [own_pid]


# ━━━ 相容與佐證 ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━


class TestCompatAndCorroboration:
    def test_old_projection_without_field_is_empty(self, root, layer, scope_engine):
        bob = make_owner(root, layer, BOB, ShareLLM(), roster=None)
        pid = share_one(bob)
        # 舊投影：metadata 沒有 subject_uids 欄位，也沒有 subject_optouts 索引檔
        for c in layer._projection_concepts(layer._semantic("scope-g42")):
            c.metadata.pop("subject_uids", None)
        assert layer.get_projection(G42, pid).subject_uids == ()
        assert layer.is_about_healthy(G42)
        got = named(scope_engine, BOB)
        assert [it.projection_id for it in got.owner_items] == [pid] and got.about_items == []

    def test_malformed_field_is_treated_as_empty(self, root, layer):
        bob = make_owner(root, layer, BOB, ShareLLM(), roster=None)
        pid = share_one(bob)
        for c in layer._projection_concepts(layer._semantic("scope-g42")):
            c.metadata["subject_uids"] = "7001002"  # 不是 list
        assert layer.get_projection(G42, pid).subject_uids == ()

    def test_owner_removed_from_subject_uids(self, root, layer):
        res = layer.project(
            G42, owner_uid=ALICE, source_concept_id="c1", source_digest="d" * 64, summary=SUMMARY,
            embedding=vec(), subject_uids=[ALICE, BOB, " ", BOB],
        )
        assert layer.get_projection(G42, res.projection_id).subject_uids == (BOB,)

    def test_about_projection_never_corroborates_subjects_own_claim(self, root, layer):
        summary = "某成員打算明天去買票"
        alice = make_owner(root, layer, ALICE, ShareLLM(summary=summary, claim_kind="intention"), roster=None)
        cid = alice.concept("明天買票", [alice.say("我打算明天去買票")])
        alice.review(cid, status="insufficient", claim_kind="intention", strength=0.5)
        assert alice.phase.run().projected == 1

        def peer(source: str, subjects) -> None:
            res = layer.project(
                G42, owner_uid=BOB, source_concept_id=source, source_digest="d" * 64,
                summary=summary, claim_kind="fact", support_strength=0.95, review_status="supported",
                embedding=vec(summary), subject_uids=subjects,
            )
            assert res.ok

        peer("c_about", [ALICE])  # 乙轉述「甲要去買票」：不是甲自述的獨立佐證
        assert alice.phase.run().corroborated == 0
        peer("c_self", [])  # 乙自己也要去買票：照常佐證
        assert alice.phase.run().corroborated == 1


def test_about_projection_does_not_leak_into_general_search(world, scope_engine):
    """一般（非指名）檢索沿用原行為：每則投影一次，不多出旁證段。"""
    got = scope_engine.retrieve_shared(SUMMARY, scope_id=GUILD42, viewer_uid=CAROL, min_similarity=0.0)
    assert all(it.relation == "owner" for it in got.items) and len(got.items) == 2
