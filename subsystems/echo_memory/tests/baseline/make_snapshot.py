"""基線快照產生工具（EM-001）。

兩種模式：
  1. 固定腳本生成（預設，可重現）：
       python -m tests.baseline.make_snapshot --out <目錄>
     以固定對話腳本 + 確定性 embedding + 確定性 fake LLM 建出
     Episode / Concept / Relation 圖譜 / Audit / patch history。
  2. 從既有 data 目錄複製（唯讀來源）：
       python -m tests.baseline.make_snapshot --from-data <data目錄> --out <目錄>
     只複製、不改動來源。注意真實資料可能含個人對話，不要提交進 repo。

預設輸出位置為 tests/baseline/fixtures/snapshot_v1/，並同時寫出
snapshot_v1.manifest.json（供還原完整性測試比對）。

注意：memory_graph.gpickle 是 networkx 版本綁定的二進位檔；
networkx 大版本升級後若無法載入，請重新執行本腳本並重生 golden
（UPDATE_GOLDEN=1 pytest tests/baseline）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.baseline._support import (  # noqa: E402
    SNAPSHOT_DIR,
    SNAPSHOT_TIMESTAMP,
    copy_from_data_dir,
    make_fake_llm,
    read_jsonl,
)

# 固定對話腳本：(id, session, user_input, agent_response, summary, salience)
EPISODE_SCRIPT = [
    ("ep_base_001", "sess_a", "我最近迷上手沖咖啡", "手沖咖啡很有趣，你喜歡什麼豆子？", "使用者迷上手沖咖啡", 0.80),
    ("ep_base_002", "sess_a", "我喜歡淺焙的衣索比亞豆", "淺焙衣索比亞有花香與果酸。", "使用者偏好淺焙衣索比亞咖啡豆", 0.70),
    ("ep_base_003", "sess_a", "我家有一隻叫小橘的貓", "小橘聽起來很可愛。", "使用者養了一隻叫小橘的貓", 0.85),
    ("ep_base_004", "sess_a", "小橘最近都不吃飼料", "可能要觀察是否生病。", "小橘最近食慾不振", 0.60),
    ("ep_base_005", "sess_b", "我妹妹考上筑波大學", "恭喜她！", "使用者的妹妹考上筑波大學", 0.90),
    ("ep_base_006", "sess_b", "她下個月要搬去日本", "需要幫忙準備行李嗎？", "妹妹下個月搬去日本", 0.65),
    ("ep_base_007", "sess_b", "我在台北的軟體公司上班", "通勤辛苦嗎？", "使用者在台北的軟體公司上班", 0.75),
    ("ep_base_008", "sess_b", "我通常凌晨兩點才睡", "作息要注意。", "使用者習慣凌晨才睡", 0.55),
    ("ep_base_009", "sess_b", "今天天氣不錯", "是啊，很適合出門。", "閒聊天氣", 0.30),
    ("ep_base_010", "sess_b", "週末想去山上走走", "記得帶外套。", "使用者週末想去爬山", 0.45),
]

# 概念腳本：(name, description, evidence_kind, source episode)
CONCEPT_SCRIPT = [
    ("手沖咖啡", "使用者喜歡手沖咖啡，偏好淺焙豆", "extractor_new_concept", "ep_base_001"),
    ("小橘", "使用者養的貓", "extractor_new_concept", "ep_base_003"),
    ("筑波大學", "使用者妹妹就讀的日本大學", "extractor_new_concept", "ep_base_005"),
    ("台北工作", "使用者在台北的軟體公司上班", "extractor_new_concept", "ep_base_007"),
    ("夜貓作息", "使用者習慣凌晨才睡", "extractor_new_concept", "ep_base_008"),
]

RELATES_TO = [("手沖咖啡", "夜貓作息"), ("小橘", "夜貓作息"), ("筑波大學", "台北工作")]


@contextmanager
def deterministic_embeddings():
    """產生器專用：以 hash 向量取代 embedding 模型（與 tests/conftest 相同函式）。"""
    from echo_memory.storage.episodic import EpisodicMemory
    from tests.conftest import _deterministic_embedding

    original = EpisodicMemory._generate_embedding
    EpisodicMemory._generate_embedding = lambda self, text: _deterministic_embedding(text)
    try:
        yield
    finally:
        EpisodicMemory._generate_embedding = original


def build_snapshot(out_dir: Path) -> dict:
    """用固定腳本在 out_dir 建出快照，回傳 manifest。"""
    from echo_memory.affect.concept_inhibitor import ConceptInhibitor
    from echo_memory.affect.concept_verifier import ConceptAssessment, ConceptVerification
    from echo_memory.engine import MemoryEngine
    from echo_memory.schemas.episode import Episode

    out_dir = Path(out_dir)
    if out_dir.exists() and any(out_dir.iterdir()):
        raise FileExistsError(f"輸出目錄非空: {out_dir}")
    out_dir.mkdir(parents=True, exist_ok=True)

    with deterministic_embeddings():
        engine = MemoryEngine(storage_path=out_dir)
        for ep_id, sess, user, agent, summary, sal in EPISODE_SCRIPT:
            engine.episodic.write(Episode(
                id=ep_id, user_input=user, agent_response=agent,
                session_id=sess, content_summary=summary,
                salience_score=sal, timestamp=SNAPSHOT_TIMESTAMP,
            ))

        inhibitor = ConceptInhibitor(semantic=engine.semantic, llm_fn=make_fake_llm())
        graph = engine.episodic.graph
        ids: dict[str, str] = {}
        for name, desc, kind, ep_id in CONCEPT_SCRIPT:
            cid, _tx = inhibitor.process_extractor_patch(
                noun=name, desc=desc, episode_id=ep_id, evidence_kind=kind,
            )
            ids[name] = cid
            graph.add_concept_node(cid, name)
            graph.add_derived_from_edge(cid, ep_id)
        for a, b in RELATES_TO:
            graph.add_relates_to_edge(ids[a], ids[b], weight=0.6, salience=0.6)

        # 對話路徑：expand（規則 approve）+ modify（fake LLM 佐證）
        conv = ConceptVerification(assessments=[
            ConceptAssessment(
                concept_id=ids["手沖咖啡"], concept_name="手沖咖啡", status="expand",
                reason="使用者提到偏好衣索比亞豆", suggested_update="偏好衣索比亞淺焙豆",
                relevance=0.9,
            ),
            ConceptAssessment(
                concept_id=ids["筑波大學"], concept_name="筑波大學", status="modify",
                reason="使用者補充妹妹已考上", suggested_update="使用者妹妹考上並將就讀的日本大學",
                relevance=0.9,
            ),
        ])
        inhibitor.process(
            conv, user_input="我妹妹考上筑波大學", llm_response="恭喜她！",
            session_id="sess_b", turn_id="ep_base_005",
        )

        # 無 LLM 的 inhibitor：modify 落入 held（只進 audit，不改概念）
        held_inhibitor = ConceptInhibitor(semantic=engine.semantic, llm_fn=None)
        held_inhibitor.process(
            ConceptVerification(assessments=[ConceptAssessment(
                concept_id=ids["台北工作"], concept_name="台北工作", status="modify",
                reason="使用者可能換工作", suggested_update="使用者準備轉職", relevance=0.8,
            )]),
            user_input="我可能要換工作", llm_response="需要聊聊嗎？",
            session_id="sess_b", turn_id="ep_base_007",
        )
        graph.save()

        audit = read_jsonl(engine.semantic.storage_path / "audit_log.jsonl")
        manifest = {
            "episode_ids": sorted(engine.episodic._episodes),
            "concept_names": sorted(c.name for c in engine.semantic.list_concepts(limit=None)),
            "audit_tx_count": len(audit),
            "held_patch_count": sum(
                1 for tx in audit for p in tx["patches"] if p["status"] == "held"
            ),
            "relates_to_edges": len(RELATES_TO),
            "derived_from_edges": len(CONCEPT_SCRIPT),
        }
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="產生 / 複製 Echo Memory 基線快照")
    parser.add_argument("--out", type=Path, default=SNAPSHOT_DIR)
    parser.add_argument("--from-data", type=Path, default=None,
                        help="改為從既有 data 目錄複製（唯讀來源）")
    parser.add_argument("--force", action="store_true", help="輸出目錄非空時先清除")
    args = parser.parse_args()

    out: Path = args.out
    if out.exists() and any(out.iterdir()):
        if not args.force:
            print(f"輸出目錄非空: {out}（加 --force 覆寫）", file=sys.stderr)
            return 1
        shutil.rmtree(out)

    if args.from_data:
        copy_from_data_dir(args.from_data, out)
        print(f"已複製 {args.from_data} -> {out}")
        return 0

    manifest = build_snapshot(out)
    manifest_path = out.parent / f"{out.name}.manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(f"已產生快照 {out}\nmanifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
