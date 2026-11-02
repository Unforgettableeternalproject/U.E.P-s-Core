"""基線測試共用工具：快照還原 / 生成、確定性 fake LLM、audit 讀取。

只使用公開介面（MemoryEngine / SemanticMemory / ConceptInhibitor 等），
不依賴內部鎖實作。
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASELINE_DIR = Path(__file__).parent
SNAPSHOT_DIR = BASELINE_DIR / "fixtures" / "snapshot_v1"
GOLDEN_PATH = BASELINE_DIR / "golden" / "recall_baseline.json"

# 快照內 Episode 的固定時間（所有 Episode 同一時間，避免排名隨時間飄移）
SNAPSHOT_TIMESTAMP = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
# 檢索時凍結 time.time() 的值（recency 與 graph edge age 的基準）
FROZEN_NOW = SNAPSHOT_TIMESTAMP.timestamp() + 3600.0

# 排除不屬於「記憶資料」的暫存／鎖檔
_IGNORED_PATTERNS = ("*.lock", "*.tmp", "*.pyc", "__pycache__")


def restore_snapshot(snapshot_dir: Path, dest_dir: Path) -> Path:
    """把快照複製到 dest_dir（不會修改 snapshot_dir）。回傳 dest_dir。"""
    if not snapshot_dir.is_dir():
        raise FileNotFoundError(f"快照不存在: {snapshot_dir}")
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    shutil.copytree(
        snapshot_dir, dest_dir,
        ignore=shutil.ignore_patterns(*_IGNORED_PATTERNS),
    )
    return dest_dir


def copy_from_data_dir(src_dir: Path, dest_dir: Path) -> Path:
    """把既有 data 目錄複製成快照（唯讀來源；來源絕不被修改）。"""
    src_dir = Path(src_dir).resolve()
    dest_dir = Path(dest_dir).resolve()
    if src_dir == dest_dir or src_dir in dest_dir.parents:
        raise ValueError("目的地不可位於來源目錄內")
    return restore_snapshot(src_dir, dest_dir)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def tx_audit(engine) -> list[dict[str, Any]]:
    """讀取 ConceptInhibitor 的 PatchTransaction audit（semantic/audit_log.jsonl）。"""
    return read_jsonl(engine.semantic.storage_path / "audit_log.jsonl")


def dream_audit(engine) -> list[dict[str, Any]]:
    """讀取 Dream 圖譜操作 audit（semantic/dream_audit_log.jsonl）。"""
    return read_jsonl(engine.semantic.storage_path / "dream_audit_log.jsonl")


def make_fake_llm(
    *,
    distill_concepts: list[dict[str, Any]] | None = None,
    reflections: list[dict[str, Any]] | None = None,
    extract_items: list[dict[str, str]] | None = None,
    corroborate: dict[str, Any] | None = None,
    reflect: dict[str, Any] | None = None,
    calls: list[str] | None = None,
):
    """依 prompt 特徵分派、回傳固定 JSON 的確定性 fake LLM。

    未知 prompt 一律回 "[]"（各模組皆會優雅跳過）。
    calls 若提供，會記錄每次命中的分類，供斷言 LLM 是否被呼叫。
    """
    distill_concepts = distill_concepts or []
    reflections = reflections or []
    extract_items = extract_items or []
    corroborate = corroborate or {"approve": True, "confidence": 0.8, "reason": "fake"}
    reflect = reflect or {"action": "none", "content": "", "reasoning": "fake", "memory_ops": []}

    def _fn(prompt: str) -> str:
        if "知識蒸餾專家" in prompt:
            kind, out = "distill", json.dumps(distill_concepts, ensure_ascii=False)
        elif "記憶反思專家" in prompt:
            kind, out = "replay", json.dumps({"reflections": reflections}, ensure_ascii=False)
        elif "提取 3-5 個關鍵概念" in prompt:
            kind, out = "extract", json.dumps(extract_items, ensure_ascii=False)
        elif "記憶系統的反思模組" in prompt:
            kind, out = "reflect", json.dumps(reflect, ensure_ascii=False)
        elif "概念變更驗證器" in prompt:
            kind, out = "corroborate", json.dumps(corroborate, ensure_ascii=False)
        else:
            kind, out = "other", "[]"
        if calls is not None:
            calls.append(kind)
        return out

    return _fn


def edge_pairs(engine, edge_type: str) -> set[frozenset[str]]:
    """回傳圖譜中指定類型邊的無向節點對集合（雙向邊只算一對）。"""
    g = engine.episodic.graph.graph
    return {
        frozenset((u, v))
        for u, v, data in g.edges(data=True)
        if data.get("edge_type") == edge_type
    }


def directed_edges(engine, edge_type: str) -> set[tuple[str, str]]:
    g = engine.episodic.graph.graph
    return {
        (u, v) for u, v, data in g.edges(data=True)
        if data.get("edge_type") == edge_type
    }


def tree_digest(root: Path) -> dict[str, str]:
    """目錄內容雜湊（相對路徑 -> sha256），用來證明來源快照未被改動。"""
    import hashlib
    digest: dict[str, str] = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            digest[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
    return digest
