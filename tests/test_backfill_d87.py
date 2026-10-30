"""
tests/test_backfill_d87.py - D87 既有投影 about_members 回填工具（scripts/backfill_d87.py）

涵蓋：dry-run 不寫檔且報告遮罩、缺名冊／名冊格式錯誤拒絕、apply 缺備份拒絕、租約被持有時中止、
舊投影補上 subject_uids、名冊外代號丟棄、無名冊的 guild 跳過、撤回／改寫的前後統計、重跑冪等、
LLM 失敗可重試、limit。全部使用假 llm_fn 與 tmp 資料，名冊成員與 uid 都是占位資料。
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from echo_memory.storage.locking import FileLease
from scripts import backfill_d87 as bf
from tests.sharing_support import G42, G43, VOICE6, Owner, ShareLLM, make_layer

UID = "1001"
BOB, CAROL = "2002", "2003"
ROSTER = {
    "42": [
        {"uid": BOB, "display_name": "乙", "aliases": ["小乙"]},
        {"uid": CAROL, "display_name": "丙", "aliases": []},
    ],
}


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class AboutLLM(ShareLLM):
    """分享審核的輸出多一個 about_members：依名冊顯示名挑代號，或直接輸出 raw_about。"""

    def __init__(self, about_names=(), raw_about=None, fail=False, **kw):
        super().__init__(**kw)
        self.about_names = tuple(about_names)
        self.raw_about = raw_about
        self.fail = fail

    def __call__(self, prompt: str) -> str:
        if self.fail:
            raise RuntimeError("llm down")
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


def scope_projections(root: Path, scope_ns: str) -> list[dict]:
    raw = json.loads((root / "namespaces" / scope_ns / "semantic" / "concepts.json").read_text(encoding="utf-8"))
    return [
        c for c in raw
        if c["status"] == "active" and (c.get("metadata") or {}).get("kind") == "projection"
    ]


def strip_subject_uids(root: Path) -> None:
    """模擬 D87 之前產生的投影：移除 metadata.subject_uids 欄位。"""
    for ns in ("scope-g42", "scope-g43"):
        path = root / "namespaces" / ns / "semantic" / "concepts.json"
        if not path.exists():
            continue
        raw = json.loads(path.read_text(encoding="utf-8"))
        for c in raw:
            (c.get("metadata") or {}).pop("subject_uids", None)
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


@pytest.fixture
def world(tmp_path: Path):
    """一位擁有者：兩個 guild 42 的舊格式投影（a、b）與一個 guild 43 的舊格式投影（c）。"""
    root = tmp_path / "data"
    layer = make_layer(root)
    owner = Owner(root, UID, layer, ShareLLM())
    cids: dict[str, str] = {}
    for key, scope in (("a", G42), ("b", G42), ("c", G43)):
        ep = owner.say(f"我聊過{key}話題", scope=scope, attrs=VOICE6)
        cids[key] = owner.concept(key, [ep])
    owner.phase.run(limit=20)
    assert len(layer.list_projections(G42)) == 2 and len(layer.list_projections(G43)) == 1
    owner.engine.close()
    strip_subject_uids(root)
    roster_path = tmp_path / "roster.json"
    roster_path.write_text(json.dumps(ROSTER, ensure_ascii=False), encoding="utf-8")
    return {"root": root, "layer": layer, "cids": cids, "tmp": tmp_path, "roster_path": roster_path}


def backup(tmp: Path) -> Path:
    b = tmp / "backup"
    b.mkdir(exist_ok=True)
    return b


def roster_of(world) -> dict:
    return bf.load_roster(world["roster_path"])


class TestRosterFile:
    @pytest.mark.parametrize("payload", [
        "[]",
        "not json",
        '{"abc": []}',
        '{"42": {}}',
        '{"42": ["x"]}',
        '{"42": [{"display_name": "乙"}]}',
        '{"42": [{"uid": "", "display_name": "乙"}]}',
        '{"42": [{"uid": "1"}]}',
        '{"42": [{"uid": "1", "display_name": "乙", "aliases": "小乙"}]}',
        '{"42": [{"uid": "1", "display_name": "乙"}, {"uid": "1", "display_name": "丙"}]}',
    ])
    def test_bad_format_rejected(self, tmp_path, payload):
        p = tmp_path / "r.json"
        p.write_text(payload, encoding="utf-8")
        with pytest.raises(bf.BackfillError):
            bf.load_roster(p)

    def test_missing_file_rejected(self, tmp_path):
        with pytest.raises(bf.BackfillError):
            bf.load_roster(tmp_path / "nope.json")
        with pytest.raises(bf.BackfillError):
            bf.load_roster(None)

    def test_valid_roster_loaded(self, world):
        assert roster_of(world) == {"42": [
            {"uid": BOB, "display_name": "乙", "aliases": ["小乙"]},
            {"uid": CAROL, "display_name": "丙", "aliases": []},
        ]}

    def test_main_refuses_without_valid_roster(self, world):
        root, tmp = world["root"], world["tmp"]
        before = snapshot(root)
        assert bf.main(["--data-root", str(root)]) == 2
        assert bf.main(["--data-root", str(root), "--roster", str(tmp / "nope.json")]) == 2
        bad = tmp / "bad.json"
        bad.write_text("[1]", encoding="utf-8")
        assert bf.main(["--data-root", str(root), "--roster", str(bad)]) == 2
        assert bf.main([
            "--data-root", str(root), "--roster", str(bad), "--apply", "--i-have-backup", str(backup(tmp)),
        ]) == 2
        assert snapshot(root) == before

    def test_run_without_roster_refused(self, world):
        with pytest.raises(bf.BackfillError):
            bf.run(world["root"])


class TestDryRun:
    def test_no_files_written_no_llm_and_masked(self, world):
        root = world["root"]
        before = snapshot(root)
        report = bf.run(root, roster=roster_of(world))
        assert snapshot(root) == before
        assert report["mode"] == "dry-run"
        assert report["roster"]["guilds"] == 1 and report["roster"]["members"] == 2
        ns = report["namespaces"][0]
        assert ns["namespace"] == "ns#1"
        assert ns["old_format_projections"] == 3
        assert ns["targets"] == 2 and ns["target_projections"] == 2
        assert ns["skipped"]["no_roster"] == 1
        assert ns["estimated_llm_calls"] == {"share_rejudge_est": 2}
        scopes = {s["scope"]: s for s in report["scopes"]}
        assert sorted(s["roster_members"] for s in scopes.values()) == [0, 2]
        dumped = json.dumps(report, ensure_ascii=False)
        for secret in (UID, BOB, CAROL, "乙", "小乙", "丙", "guild:42", "scope-g42", *world["cids"].values()):
            assert secret not in dumped

    def test_main_dry_run_needs_no_llm_and_report_is_masked(self, world, capsys):
        root = world["root"]
        before = snapshot(root)
        out = world["tmp"] / "out" / "report.json"
        assert bf.main([
            "--data-root", str(root), "--roster", str(world["roster_path"]), "--report", str(out),
        ]) == 0
        assert snapshot(root) == before
        assert json.loads(out.read_text(encoding="utf-8"))["totals"]["targets"] == 2
        printed = capsys.readouterr().out
        for secret in (UID, BOB, CAROL, "乙", "丙"):
            assert secret not in printed

    def test_report_inside_data_root_rejected(self, world):
        root = world["root"]
        assert bf.main([
            "--data-root", str(root), "--roster", str(world["roster_path"]), "--report", str(root / "r.json"),
        ]) == 2


class TestApplyGuards:
    def test_apply_without_backup_refused(self, world):
        root = world["root"]
        before = snapshot(root)
        with pytest.raises(bf.BackfillError):
            bf.run(root, roster=roster_of(world), apply=True, backup=None, llm_fn=AboutLLM())
        with pytest.raises(bf.BackfillError):
            bf.run(root, roster=roster_of(world), apply=True, backup=world["tmp"] / "nope", llm_fn=AboutLLM())
        assert bf.main(["--data-root", str(root), "--roster", str(world["roster_path"]), "--apply"]) == 2
        assert snapshot(root) == before

    def test_lease_held_aborts_without_changes(self, world):
        root = world["root"]
        holder = FileLease(root / "namespaces" / UID, "dream")
        ok, _ = holder.try_acquire(owner="bot")
        assert ok
        before = snapshot(root)
        llm = AboutLLM(about_names=["乙"])
        with pytest.raises(bf.BackfillError, match="租約"):
            bf.run(root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        assert snapshot(root) == before and llm.share_calls == []
        holder.release()


class TestApply:
    def test_old_projections_get_subject_uids(self, world):
        root, layer = world["root"], world["layer"]
        llm = AboutLLM(about_names=["乙"])
        report = bf.run(root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)

        g42 = layer.list_projections(G42)
        assert len(g42) == 2 and all(v.subject_uids == (BOB,) for v in g42)
        # 欄位存在於磁碟 metadata（新格式）
        assert all(c["metadata"]["subject_uids"] == [BOB] for c in scope_projections(root, "scope-g42"))
        # 沒有名冊的 guild 43：不重判、投影維持舊格式
        g43 = scope_projections(root, "scope-g43")
        assert len(g43) == 1 and "subject_uids" not in g43[0]["metadata"]
        assert len(llm.share_calls) == 2

        t = report["totals"]
        assert t["projections_with_subjects"] == 2 and t["subjects_total"] == 2
        assert t["subject_distribution"] == {"1": 2}
        assert sum(t["outcomes"].values()) == 2 and "unchanged_pending" not in t["outcomes"]
        assert t["projection_status_before"] and t["projection_status_after"]
        dumped = json.dumps(report, ensure_ascii=False)
        for secret in (UID, BOB, CAROL, "乙", *world["cids"].values()):
            assert secret not in dumped

        # audit 先於結果；登記簿只記有處理的概念
        audit = [
            json.loads(x) for x in
            (root / "namespaces" / UID / "semantic" / bf.AUDIT_NAME).read_text(encoding="utf-8").splitlines()
        ]
        assert audit[0]["event"] == "run_start"
        for key in ("a", "b"):
            cid = world["cids"][key]
            i = next(n for n, a in enumerate(audit) if a.get("concept_id") == cid and a["event"] == "intent")
            j = next(n for n, a in enumerate(audit) if a.get("concept_id") == cid and a["event"] == "result")
            assert i < j
        assert set(bf.load_ledger(root / "namespaces" / UID)) == {world["cids"]["a"], world["cids"]["b"]}

    def test_codes_outside_roster_are_dropped(self, world):
        root, layer = world["root"], world["layer"]
        llm = AboutLLM(raw_about=["m1", "m9", BOB, "x", 7, None, "m1"])
        bf.run(root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        assert all(v.subject_uids == (BOB,) for v in layer.list_projections(G42))
        # uid 不進 prompt
        assert all(BOB not in p and CAROL not in p for p in llm.share_calls)

    def test_empty_about_still_converts_to_new_format(self, world):
        root, layer = world["root"], world["layer"]
        report = bf.run(
            root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=AboutLLM(),
        )
        assert all(v.subject_uids == () for v in layer.list_projections(G42))
        assert all("subject_uids" in c["metadata"] for c in scope_projections(root, "scope-g42"))
        assert report["totals"]["subject_distribution"] == {"0": 2}
        assert report["totals"]["projections_with_subjects"] == 0

    def test_rejudge_can_revoke(self, world):
        root, layer = world["root"], world["layer"]
        llm = AboutLLM(about_names=["乙"], sensitivity="sensitive")
        report = bf.run(root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        assert layer.list_projections(G42) == []
        assert report["totals"]["outcomes"] == {"revoked": 2}
        assert len(layer.list_projections(G43)) == 1  # 其他 guild 不受影響

    def test_rejudge_can_rewrite(self, world):
        root, layer = world["root"], world["layer"]
        llm = AboutLLM(about_names=["乙"], summary="改寫後的摘要")
        report = bf.run(root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        assert report["totals"]["outcomes"] == {"rewritten": 2}
        assert {v.summary for v in layer.list_projections(G42)} == {"改寫後的摘要"}

    def test_rerun_is_idempotent(self, world):
        root = world["root"]
        bk = backup(world["tmp"])
        bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=AboutLLM(about_names=["乙"]))
        after_first = snapshot(root)
        llm2 = AboutLLM(about_names=["乙"])
        report = bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=llm2)
        assert report["namespaces"][0]["targets"] == 0
        assert llm2.share_calls == []
        changed = {k for k, v in snapshot(root).items() if after_first.get(k) != v}
        assert all(k.endswith(".dream-lease.json") for k in changed)

    def test_llm_failure_keeps_old_projection_and_can_retry(self, world):
        root, layer = world["root"], world["layer"]
        bk = backup(world["tmp"])
        report = bf.run(
            root, roster=roster_of(world), apply=True, backup=bk, llm_fn=AboutLLM(fail=True),
        )
        assert report["totals"]["outcomes"] == {"unchanged_pending": 2}
        assert bf.load_ledger(root / "namespaces" / UID) == {}
        assert all("subject_uids" not in c["metadata"] for c in scope_projections(root, "scope-g42"))
        assert len(layer.list_projections(G42)) == 2
        bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=AboutLLM(about_names=["乙"]))
        assert all(v.subject_uids == (BOB,) for v in layer.list_projections(G42))

    def test_limit_counts_source_concepts(self, world):
        root = world["root"]
        llm = AboutLLM(about_names=["乙"])
        report = bf.run(
            root, roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm, limit=1,
        )
        assert report["totals"]["targets"] == 1 and len(llm.share_calls) == 1
        assert bf.run(root, roster=roster_of(world))["namespaces"][0]["targets"] == 1

    def test_unknown_namespace_rejected(self, world):
        with pytest.raises(bf.BackfillError):
            bf.run(world["root"], roster=roster_of(world), namespaces="9999")


class TestPromptAndRejudgeAll:
    def test_share_prompt_keeps_public_proper_nouns(self, world):
        llm = AboutLLM(about_names=["乙"])
        bf.run(world["root"], roster=roster_of(world), apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        prompt = llm.share_calls[0]
        assert "保留公開專有名詞" in prompt
        assert "不要抽象化" in prompt

    def test_rejudge_all_selects_projections_with_subject_uids(self, world):
        root = world["root"]
        bk = backup(world["tmp"])
        bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=AboutLLM(about_names=["乙"]))
        # 原回填已完成：預設模式沒有目標
        assert bf.run(root, roster=roster_of(world))["namespaces"][0]["targets"] == 0
        # rejudge-all dry-run：選中已有 subject_uids 的 guild 42 投影，且不寫任何檔案
        before = snapshot(root)
        dry = bf.run(root, roster=roster_of(world), rejudge_all=True)
        assert snapshot(root) == before
        ns = dry["namespaces"][0]
        assert dry["rejudge_all"] is True and ns["targets"] == 2 and ns["skipped"]["no_roster"] == 1

    def test_rejudge_all_idempotent_with_separate_ledger(self, world):
        root = world["root"]
        bk = backup(world["tmp"])
        ns_dir = root / "namespaces" / UID
        bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=AboutLLM(about_names=["乙"]))
        orig_ledger = (ns_dir / "semantic" / bf.LEDGER_NAME).read_bytes()
        llm = AboutLLM(about_names=["乙"], summary="保留遊戲名的新摘要")
        report = bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=llm, rejudge_all=True)
        assert len(llm.share_calls) == 2 and report["totals"]["outcomes"].get("rewritten") == 2
        assert {v.summary for v in world["layer"].list_projections(G42)} == {"保留遊戲名的新摘要"}
        # 原登記簿不變；另寫 rejudge 登記簿與 audit
        assert (ns_dir / "semantic" / bf.LEDGER_NAME).read_bytes() == orig_ledger
        assert set(bf.load_ledger(ns_dir, rejudge_all=True)) == {world["cids"]["a"], world["cids"]["b"]}
        assert (ns_dir / "semantic" / bf.REJUDGE_AUDIT_NAME).exists()
        # 再跑一次：冪等
        llm2 = AboutLLM(about_names=["乙"])
        again = bf.run(root, roster=roster_of(world), apply=True, backup=bk, llm_fn=llm2, rejudge_all=True)
        assert again["namespaces"][0]["targets"] == 0 and llm2.share_calls == []
