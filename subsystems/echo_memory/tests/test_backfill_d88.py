"""
tests/test_backfill_d88.py - D88 舊資料一次性重審工具（scripts/backfill_d88.py）

涵蓋：dry-run 不寫檔且報告遮罩、apply 缺備份拒絕、租約被持有時中止、
insufficient -> not_required 並更新共享投影、重跑冪等、不碰其他狀態、limit、held 可重試。
全部使用假 llm_fn 與 conftest 的確定性 embedding。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from echo_memory.storage.locking import FileLease
from scripts import backfill_d88 as bf
from tests.sharing_support import G42, VOICE6, Owner, ShareLLM, make_layer

UID = "1001"


def snapshot(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


class BackfillLLM:
    """審核與分享兩種 prompt 都回結構化 JSON；記錄呼叫。"""

    def __init__(self, unit_ids: dict[str, list[str]] | None = None, review_class: str = "general",
                 fail: bool = False):
        self.share = ShareLLM(corroboration_class=review_class)
        self.review_class = review_class
        self.fail = fail
        self.review_calls = 0
        self.unit_ids = unit_ids or {}

    @property
    def share_calls(self) -> int:
        return len(self.share.share_calls)

    def __call__(self, prompt: str) -> str:
        if self.fail:
            raise RuntimeError("llm down")
        if "分享審核員" in prompt:
            return self.share(prompt)
        self.review_calls += 1
        ids = next((u for u in self.unit_ids.values() if any(i in prompt for i in u)), [])
        ids = [i for i in ids if i in prompt]
        return json.dumps({
            "status": "supported", "claim_kind": "preference", "cited_unit_ids": ids,
            "corroborating_unit_ids": [], "scope": {"subject_ref": "user", "context": None},
            "corroboration_class": self.review_class, "reason": "測試",
        }, ensure_ascii=False)


@pytest.fixture
def world(tmp_path: Path):
    """一位擁有者：兩個舊 insufficient 概念（有投影）、supported／contradicted／未審核各一。"""
    root = tmp_path / "data"
    layer = make_layer(root)
    owner = Owner(root, UID, layer, ShareLLM(corroboration_class=None))
    units: dict[str, list[str]] = {}
    cids: dict[str, str] = {}
    for key in ("old_a", "old_b", "ok", "bad", "fresh"):
        ep = owner.say(f"我常聊{key}話題", attrs=VOICE6)
        cids[key] = owner.concept(key, [ep])
        units[key] = owner.unit_ids(ep)
    owner.phase.run(limit=20)  # 舊行為：未分類 -> 投影為 insufficient
    for key in ("old_a", "old_b"):
        owner.review(cids[key], status="insufficient")
    owner.review(cids["ok"], status="supported")
    owner.review(cids["bad"], status="contradicted", strength=0.0)
    assert {v.review_status for v in layer.list_projections(G42)} == {"insufficient"}
    owner.engine.close()
    return {"root": root, "layer": layer, "cids": cids, "units": units, "tmp": tmp_path}


def backup(tmp: Path) -> Path:
    b = tmp / "backup"
    b.mkdir()
    return b


def concept_states(root: Path) -> dict[str, dict]:
    raw = json.loads((root / "namespaces" / UID / "semantic" / "concepts.json").read_text(encoding="utf-8"))
    return {c["name"]: c for c in raw}


class TestDryRun:
    def test_no_files_written_and_masked(self, world):
        root = world["root"]
        before = snapshot(root)
        report = bf.run(root)
        assert snapshot(root) == before
        assert report["mode"] == "dry-run"
        ns = report["namespaces"][0]
        assert ns["namespace"] == "ns#1" and ns["targets"] == 2
        assert ns["projections_affected"] == 2 and ns["projections_live_in_scope"] == 2
        assert ns["estimated_llm_calls"] == {"review": 2, "share_rejudge_max": 2}
        dumped = json.dumps(report, ensure_ascii=False)
        assert UID not in dumped
        for cid in world["cids"].values():
            assert cid not in dumped
        for name in world["cids"]:
            assert name not in dumped

    def test_main_dry_run_needs_no_llm_and_writes_report_outside(self, world, capsys):
        root = world["root"]
        before = snapshot(root)
        out = world["tmp"] / "out" / "report.json"
        assert bf.main(["--data-root", str(root), "--report", str(out)]) == 0
        assert snapshot(root) == before
        assert json.loads(out.read_text(encoding="utf-8"))["totals"]["targets"] == 2
        assert UID not in capsys.readouterr().out

    def test_report_inside_data_root_rejected(self, world):
        root = world["root"]
        assert bf.main(["--data-root", str(root), "--report", str(root / "r.json")]) == 2


class TestApplyGuards:
    def test_apply_without_backup_refused(self, world):
        root = world["root"]
        before = snapshot(root)
        with pytest.raises(bf.BackfillError):
            bf.run(root, apply=True, backup=None, llm_fn=BackfillLLM())
        with pytest.raises(bf.BackfillError):
            bf.run(root, apply=True, backup=world["tmp"] / "nope", llm_fn=BackfillLLM())
        assert bf.main(["--data-root", str(root), "--apply"]) == 2
        assert bf.main(["--data-root", str(root), "--apply", "--i-have-backup", str(world["tmp"] / "nope")]) == 2
        assert snapshot(root) == before

    def test_lease_held_aborts_without_changes(self, world):
        root = world["root"]
        holder = FileLease(root / "namespaces" / UID, "dream")
        ok, _ = holder.try_acquire(owner="bot")
        assert ok
        before = snapshot(root)
        llm = BackfillLLM(world["units"])
        with pytest.raises(bf.BackfillError, match="租約"):
            bf.run(root, apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        assert snapshot(root) == before and llm.review_calls == 0
        assert bf.run(root)["namespaces"][0]["lease"]["held"] is True
        holder.release()


class TestApply:
    def test_insufficient_becomes_not_required_and_projection_follows(self, world):
        root, layer = world["root"], world["layer"]
        llm = BackfillLLM(world["units"], review_class="general")
        report = bf.run(root, apply=True, backup=backup(world["tmp"]), llm_fn=llm)

        states = concept_states(root)
        for key in ("old_a", "old_b"):
            review = states[key]["evidence_review"]
            assert review["status"] == "not_required" and review["corroboration_class"] == "general"
        # 投影重判：仍為每個概念一筆有效投影，狀態更新為 not_required
        views = layer.list_projections(G42)
        assert sorted(v.review_status for v in views) == ["insufficient"] * 3 + ["not_required"] * 2
        ns = report["namespaces"][0]
        assert ns["apply"]["transitions"] == {"insufficient->not_required": 2}
        assert ns["apply"]["outcomes"]["reviewed"] == 2
        assert ns["apply"]["share"]["final"] == 2
        assert ns["status"]["insufficient"] == 2 and ns["status_after"]["insufficient"] == 0
        assert ns["status_after"]["not_required"] == 2
        assert report["totals"]["status_after"]["not_required"] == 2
        assert llm.review_calls == 2 and llm.share_calls >= 2

        # 被重審概念的投影：source 概念 -> 有效投影狀態皆為 not_required
        by_source = {}
        for p in json.loads(
            (root / "namespaces" / "scope-g42" / "semantic" / "concepts.json").read_text(encoding="utf-8")
        ):
            m = p["metadata"]
            if p["status"] == "active" and m.get("kind") == "projection":
                by_source.setdefault(m["source_concept_id"], []).append(m["review_status"])
        for key in ("old_a", "old_b"):
            assert by_source[world["cids"][key]] == ["not_required"]

        # audit 先於結果；登記簿記錄處理過的概念
        audit = [
            json.loads(x) for x in
            (root / "namespaces" / UID / "semantic" / bf.AUDIT_NAME).read_text(encoding="utf-8").splitlines()
        ]
        events = [a["event"] for a in audit]
        assert events[0] == "run_start"
        for cid in (world["cids"]["old_a"], world["cids"]["old_b"]):
            i = next(n for n, a in enumerate(audit) if a.get("concept_id") == cid and a["event"] == "intent")
            j = next(n for n, a in enumerate(audit) if a.get("concept_id") == cid and a["event"] == "result")
            assert i < j
        ledger = bf.load_ledger(root / "namespaces" / UID)
        assert set(ledger) == {world["cids"]["old_a"], world["cids"]["old_b"]}
        assert all(e["share_done"] and e["to"] == "not_required" for e in ledger.values())

    def test_rerun_is_idempotent(self, world):
        root = world["root"]
        bk = backup(world["tmp"])
        llm = BackfillLLM(world["units"])
        bf.run(root, apply=True, backup=bk, llm_fn=llm)
        after_first = snapshot(root)
        llm2 = BackfillLLM(world["units"])
        report = bf.run(root, apply=True, backup=bk, llm_fn=llm2)
        assert report["namespaces"][0]["targets"] == 0
        assert llm2.review_calls == 0 and llm2.share_calls == 0
        assert concept_states(root)["old_a"]["evidence_review"]["status"] == "not_required"
        # 重跑不改任何概念／投影資料（只允許租約檔變動）
        changed = {k for k, v in snapshot(root).items() if after_first.get(k) != v}
        assert all(k.endswith(".dream-lease.json") for k in changed)

    def test_other_states_untouched(self, world):
        root = world["root"]
        before = concept_states(root)
        llm = BackfillLLM(world["units"])
        bf.run(root, apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        after = concept_states(root)
        for key in ("ok", "bad", "fresh"):
            assert after[key]["evidence_review"] == before[key]["evidence_review"]
            assert after[key]["version"] == before[key]["version"]
        assert after["ok"]["evidence_review"]["status"] == "supported"
        assert after["bad"]["evidence_review"]["status"] == "contradicted"
        assert not after["fresh"]["evidence_review"]
        assert llm.review_calls == 2

    def test_self_fact_stays_insufficient_without_rejudge(self, world):
        root = world["root"]
        llm = BackfillLLM(world["units"], review_class="self_fact")
        report = bf.run(root, apply=True, backup=backup(world["tmp"]), llm_fn=llm)
        ns = report["namespaces"][0]
        assert ns["apply"]["transitions"] == {"insufficient->insufficient": 2}
        assert ns["apply"]["share"]["rejudged"] == 0 and llm.share_calls == 0
        states = concept_states(root)
        assert states["old_a"]["evidence_review"]["corroboration_class"] == "self_fact"
        # 已有 class 的 insufficient 不再列入目標
        assert bf.run(root)["namespaces"][0]["targets"] == 0

    def test_limit_caps_reviews(self, world):
        root = world["root"]
        llm = BackfillLLM(world["units"])
        report = bf.run(root, apply=True, backup=backup(world["tmp"]), llm_fn=llm, limit=1)
        assert report["namespaces"][0]["apply"]["outcomes"]["reviewed"] == 1
        assert llm.review_calls == 1
        assert bf.run(root)["namespaces"][0]["targets"] == 1

    def test_llm_failure_holds_and_can_retry(self, world):
        root = world["root"]
        bk = backup(world["tmp"])
        report = bf.run(root, apply=True, backup=bk, llm_fn=BackfillLLM(world["units"], fail=True))
        ns = report["namespaces"][0]
        assert ns["apply"]["outcomes"]["held"] == 2 and report["totals"]["hold"] == 2
        assert bf.load_ledger(root / "namespaces" / UID) == {}
        assert concept_states(root)["old_a"]["evidence_review"]["status"] == "insufficient"
        llm = BackfillLLM(world["units"])
        bf.run(root, apply=True, backup=bk, llm_fn=llm)
        assert concept_states(root)["old_a"]["evidence_review"]["status"] == "not_required"

    def test_unknown_namespace_rejected(self, world):
        with pytest.raises(bf.BackfillError):
            bf.run(world["root"], namespaces="9999")
