"""
M6 結構護欄：以 AST／原始碼掃描驗證邊界，不啟動任何東西、不載入模型。

守住的邊界：
  1. echo_memory 沒有任何 persona／base_personality／character 寫入，也不 import llm_integration；
  2. action_queue 是白名單制（未註冊 DENY），已註冊 action 不含 identity／persona／presence／task 的放行；
  3. Self Model 寫入 API 只在 Gate／patch pipeline 被引用，沒有旁路 writer；
  4. events／emitter 不碰 Core、網路、子行程，也不 import storage 的 Writer；
  5. 所有 emit_* 呼叫都是單獨的 Expr 語句（回傳值不被使用）。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_PRODUCTION = [ROOT / "echo_memory", ROOT / "llm_integration", ROOT / "mcp_server"]
_PRODUCTION_FILES = [ROOT / "Entry.py"]

_PERSONA_TOKENS = {"persona", "personality", "character"}
_EVENT_MODULES = [
    ROOT / "echo_memory" / "schemas" / "events.py",
    ROOT / "echo_memory" / "affect" / "event_emitter.py",
]
_FORBIDDEN_IMPORT_ROOTS = {"requests", "httpx", "subprocess", "socket", "aiohttp", "urllib3"}


def _py_files(dirs, extra=()):
    files = [p for d in dirs for p in sorted(d.rglob("*.py")) if "__pycache__" not in p.parts]
    return files + [p for p in extra if p.exists()]


def _parse(path: Path) -> ast.AST:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _identifier_tokens(name: str) -> set[str]:
    return {t for t in name.lower().split("_") if t}


# ─── 1. 不碰 Persona ─────────────────────────────────────────────────────────

def test_echo_memory_never_writes_persona_or_imports_llm_config():
    offenders: list[str] = []
    for path in _py_files([ROOT / "echo_memory"]):
        tree = _parse(path)
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Name):
                names.append(node.id)
            elif isinstance(node, ast.Attribute):
                names.append(node.attr)
            elif isinstance(node, ast.arg):
                names.append(node.arg)
            elif isinstance(node, ast.keyword) and node.arg:
                names.append(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.append(node.name)
            for n in names:
                if _identifier_tokens(n) & _PERSONA_TOKENS:
                    offenders.append(f"{_rel(path)}:{getattr(node, 'lineno', '?')} 識別字 {n}")
            # 字串形式的寫入：setattr(x, "persona", ...)／d["character"] = ...
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.lower() in _PERSONA_TOKENS | {"base_personality"}:
                    offenders.append(f"{_rel(path)}:{node.lineno} 字串 {node.value!r}")
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "llm_integration":
                        offenders.append(f"{_rel(path)}:{node.lineno} import {alias.name}")
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "llm_integration":
                offenders.append(f"{_rel(path)}:{node.lineno} from {node.module}")
    assert offenders == []


# ─── 2. action_queue 白名單 ──────────────────────────────────────────────────

def _action_permission_literal() -> dict[str, str]:
    tree = _parse(ROOT / "echo_memory" / "affect" / "action_queue.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "_ACTION_PERMISSIONS":
            value = node.value
            assert isinstance(value, ast.Dict)
            return {
                k.value: v.attr  # type: ignore[union-attr]
                for k, v in zip(value.keys, value.values)
            }
    raise AssertionError("找不到 _ACTION_PERMISSIONS")


def test_action_queue_is_allowlist_with_default_deny():
    from echo_memory.affect import action_queue as aq

    assert aq._DEFAULT_PERMISSION == aq.ActionPermission.DENY
    # enqueue 必須用 _DEFAULT_PERMISSION 當 .get 的預設，不得寫回 CONFIRM
    src = (ROOT / "echo_memory" / "affect" / "action_queue.py").read_text(encoding="utf-8")
    assert "_ACTION_PERMISSIONS.get(action_type, _DEFAULT_PERMISSION)" in src
    assert "ActionPermission.CONFIRM\n        )" not in src


def test_registered_actions_never_open_identity_persona_presence_task():
    perms = _action_permission_literal()
    assert perms["modify_identity"] == "DENY" and perms["modify_persona"] == "DENY"
    risky = {"identity", "persona", "presence", "task", "character", "personality"}
    for name, perm in perms.items():
        if _identifier_tokens(name) & risky:
            assert perm == "DENY", f"{name} 不得放行"
    # 非 DENY 的白名單只有記憶內部操作
    allowed = {n for n, p in perms.items() if p != "DENY"}
    assert allowed == {
        "adjust_salience", "tag_concept", "add_note",
        "merge_concepts", "archive_episode", "create_concept",
    }


# ─── 3. Self Model 寫入 API 只在 Gate／patch pipeline ───────────────────────

_SELF_MODEL_WRITERS = {"process_self_model_patch", "_apply_self_model", "_rollback_self_model"}
_GATE_FILES = {
    "echo_memory/affect/self_model_gate.py",
    "echo_memory/affect/concept_inhibitor.py",
}


def test_self_model_write_api_is_referenced_only_by_gate_pipeline():
    offenders: list[str] = []
    for path in _py_files(_PRODUCTION, _PRODUCTION_FILES):
        rel = _rel(path)
        if rel in _GATE_FILES:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Attribute) and node.attr in _SELF_MODEL_WRITERS:
                offenders.append(f"{rel}:{node.lineno} {node.attr}")
            if isinstance(node, ast.Name) and node.id in _SELF_MODEL_WRITERS:
                offenders.append(f"{rel}:{node.lineno} {node.id}")
    assert offenders == []


def test_no_direct_metadata_self_model_writes_outside_gate():
    """metadata["self_model"] 的 Store／pop／setdefault 只能出現在 Gate 檔案。"""
    offenders: list[str] = []
    for path in _py_files(_PRODUCTION, _PRODUCTION_FILES):
        rel = _rel(path)
        if rel in _GATE_FILES:
            continue
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, (ast.Store, ast.Del))
                and isinstance(node.slice, ast.Constant)
                and node.slice.value == "self_model"
            ):
                offenders.append(f"{rel}:{node.lineno} 直接寫 self_model")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"pop", "setdefault", "update"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "self_model"
            ):
                offenders.append(f"{rel}:{node.lineno} {node.func.attr}('self_model')")
    assert offenders == []


# ─── 4. events／emitter 的 import 邊界 ──────────────────────────────────────

def _imports(tree: ast.AST) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out += [(a.name, node.lineno) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, node.lineno))
    return out


@pytest.mark.parametrize("path", _EVENT_MODULES, ids=_rel)
def test_event_modules_import_no_core_network_or_subprocess(path):
    tree = _parse(path)
    offenders: list[str] = []
    for module, lineno in _imports(tree):
        parts = module.lower().split(".")
        if parts[0] in _FORBIDDEN_IMPORT_ROOTS:
            offenders.append(f"{lineno} import {module}")
        if parts[0] in {"core", "uep_core"} or "core" in parts[:1]:
            offenders.append(f"{lineno} import {module}（Core）")
        if parts[:2] == ["echo_memory", "storage"]:
            offenders.append(f"{lineno} import {module}（storage 寫入端）")
        if parts[0] == "llm_integration":
            offenders.append(f"{lineno} import {module}")
    # os.system／os.popen／os.exec* 呼叫
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and (node.func.attr in {"system", "popen", "spawnl", "startfile"} or node.func.attr.startswith("exec"))
        ):
            offenders.append(f"{node.lineno} os.{node.func.attr}")
    assert offenders == []


def test_emitter_imports_no_storage_writer():
    names: list[str] = []
    for node in ast.walk(_parse(ROOT / "echo_memory" / "affect" / "event_emitter.py")):
        if isinstance(node, ast.ImportFrom):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.Import):
            names += [a.name for a in node.names]
    assert [n for n in names if n.endswith("Writer")] == []


# ─── 5. emit_* 只能是 Expr 語句 ──────────────────────────────────────────────

def test_emit_calls_are_bare_expression_statements():
    offenders: list[str] = []
    seen = 0
    for path in _py_files(_PRODUCTION, _PRODUCTION_FILES):
        tree = _parse(path)
        expr_calls = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name.startswith("emit_"):
                seen += 1
                if id(node) not in expr_calls:
                    offenders.append(f"{_rel(path)}:{node.lineno} {name} 的回傳值被使用")
    assert seen >= 7, "應至少掃到各觸發點的 emit_* 呼叫"
    assert offenders == []


def test_new_modules_are_registered_in_architecture_ban():
    src = (ROOT / "tests" / "test_evidence_review_golden.py").read_text(encoding="utf-8")
    for rel in (
        "echo_memory/schemas/events.py",
        "echo_memory/affect/event_emitter.py",
        "echo_memory/affect/self_model_gate.py",
    ):
        assert f'"{rel}"' in src


# ─── 6. Echo 端 schema 不含 Core Gateway 的信任欄位 ─────────────────────────────

_TRUST_FIELD_NAMES = {
    "event_id", "received_at", "source", "priority", "actor_id", "journal_seq", "persona_id",
}


def test_echo_event_schema_has_no_trust_fields():
    tree = _parse(ROOT / "echo_memory" / "schemas" / "events.py")
    fields: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            fields += [
                n.target.id for n in node.body
                if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
            ]
        # payload dict literal 的 key 也不得是信任欄位
        if isinstance(node, ast.Dict):
            fields += [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
        if isinstance(node, ast.keyword) and node.arg:
            fields.append(node.arg)
    assert not (set(fields) & _TRUST_FIELD_NAMES)
    draft_fields = [
        n.target.id
        for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name == "EventDraft"
        for n in c.body if isinstance(n, ast.AnnAssign)
    ]
    assert draft_fields == ["event_type", "payload", "idempotency_key", "scope_ref"]


# ─── 7. 審查補強：dict literal 與整包 metadata 賦值 ────────────────────────────

# reflection_executor 的 rollback 只把「先前快照」放回 primary.metadata，不產生新標記
_METADATA_ASSIGN_ALLOWED = _GATE_FILES | {"echo_memory/affect/reflection_executor.py"}


def test_no_self_model_dict_literal_outside_gate():
    offenders: list[str] = []
    for path in _py_files(_PRODUCTION, _PRODUCTION_FILES):
        rel = _rel(path)
        if rel in _GATE_FILES:
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Dict):
                for k in node.keys:
                    if isinstance(k, ast.Constant) and k.value == "self_model":
                        offenders.append(f"{rel}:{node.lineno} dict literal self_model")
    assert offenders == []


def test_no_whole_metadata_assignment_outside_gate():
    offenders: list[str] = []
    for path in _py_files(_PRODUCTION, _PRODUCTION_FILES):
        rel = _rel(path)
        if rel in _METADATA_ASSIGN_ALLOWED:
            continue
        for node in ast.walk(_parse(path)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for t in targets:
                if isinstance(t, ast.Attribute) and t.attr == "metadata":
                    offenders.append(f"{rel}:{node.lineno} .metadata 整包賦值")
    assert offenders == []
