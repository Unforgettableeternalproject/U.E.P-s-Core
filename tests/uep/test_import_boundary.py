"""uep 套件與 legacy 的 import 邊界檢查（N1 後；D104／D105）。

- legacy（core／modules／utils／devtools／Entry）一律禁止。
- ``echo_stream`` 在 ``uep/`` 內全面禁止：ES 的契約與核心已併入 ``uep/stream/``（N1），ES repo 轉為 bot 維護線。
- 子系統（``echo_memory``、``echo_stt``、``echo_thought_core``、``echo_tts``、``llm_integration``、``mcp_server``）只允許
  ``uep/stream/adapters/`` 與 ``uep/echo/*_wiring.py`` import；其餘模組只認 Protocol 與 fake。
- ``uep/stream/`` 單向依賴：不得 import ``uep`` 其他部分（絕對與逸出本套件的相對 import 都算）；除 ``adapters/`` 外不得 import 子系統。
- Orchestrator、組裝層與 services 不依賴 ``uep.stream``（沒有安裝推理依賴的環境也能組裝）。
- 反向規則（子系統遷入 Core，ADR-M1）：``subsystems/`` 內不得 import ``uep``。
- 決策層與 Agent 層的單向白名單（DL-6a）：``uep/decision`` 對 ``uep.agent`` 只准 import ``uep.agent.trigger``；
  ``uep/agent`` 對 ``uep.decision`` 只准 import ``uep.decision.loop_consult`` 與 ``uep.decision.contracts``；
  被 agent 引用的 ``loop_consult.py``、``contracts.py`` 對 ``uep.agent`` 至多 import ``uep.agent.trigger``（防循環）。
"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
STREAM_DIR = UEP_ROOT / "stream"
LEGACY_NAMES = {"core", "modules", "utils", "devtools", "Entry"}
# N1：echo_stream 在 uep/ 內完全禁止（無例外）
RETIRED_NAMES = {"echo_stream"}
# 子系統頂層套件：只有 uep/stream/adapters/ 與 uep/echo/*_wiring.py 可以 import
SUBSYSTEM_NAMES = {"echo_memory", "echo_stt", "echo_thought_core", "echo_tts", "llm_integration", "mcp_server"}

# 反向規則用：子系統目錄
SUBSYSTEMS_ROOT = UEP_ROOT.parent / "subsystems"
# 掃描子系統目錄時略過的目錄（虛擬環境、快取、建置輸出）
_SKIP_DIR_NAMES = {"env", "venv", ".venv", "build", "dist", "node_modules", "__pycache__"}


def _imported_top_levels(path: Path) -> set[str]:
    return _imports_in_source(path.read_text(encoding="utf-8"), str(path))


def _imports_in_source(source: str, filename: str = "<memory>") -> set[str]:
    tree = ast.parse(source, filename=filename)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _imported_modules(path: Path) -> set[str]:
    """絕對 import 的完整模組名稱（``from uep import stream`` 視為 ``uep.stream``）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            if node.module == "uep":
                names.update(f"uep.{alias.name}" for alias in node.names)
    return names


def _escaping_relative_imports(path: Path, package_root: Path) -> list[int]:
    """``package_root`` 底下的檔案中，相對 import 逸出 ``package_root`` 套件的行號。"""
    depth = len(path.relative_to(package_root).parts) - 1  # 檔案所在子目錄深度；0＝直接在 package_root 下
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [
        node.lineno for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level > depth + 1
    ]


def _retired_violations(root: Path) -> list[str]:
    """``root`` 底下 import 了已退役的 echo_stream 的檔案（無例外）。"""
    return [
        f"{path.relative_to(root)}: {sorted(hit)}"
        for path in sorted(root.rglob("*.py"))
        if (hit := _imported_top_levels(path) & RETIRED_NAMES)
    ]


def _subsystem_import_allowed(path: Path, root: Path) -> bool:
    """子系統 import 的白名單：``uep/stream/adapters/**`` 與 ``uep/echo/*_wiring.py``（``root`` 為 uep 套件根）。"""
    rel = path.relative_to(root)
    if rel.parts[:2] == ("stream", "adapters"):
        return True
    return rel.parts[0] == "echo" and len(rel.parts) == 2 and rel.name.endswith("_wiring.py")


def _subsystem_violations(root: Path) -> list[str]:
    return [
        f"{path.relative_to(root)}: {sorted(hit)}"
        for path in sorted(root.rglob("*.py"))
        if not _subsystem_import_allowed(path, root) and (hit := _imported_top_levels(path) & SUBSYSTEM_NAMES)
    ]


def _stream_violations(stream_root: Path) -> list[str]:
    """``uep/stream/`` 不得依賴 ``uep`` 其他部分；``adapters/`` 以外不得 import 子系統。"""
    violations = []
    for path in sorted(stream_root.rglob("*.py")):
        rel = path.relative_to(stream_root)
        tops = _imported_top_levels(path)
        if "uep" in tops:
            violations.append(f"{rel}: 絕對 import uep")
        for lineno in _escaping_relative_imports(path, stream_root):
            violations.append(f"{rel}:{lineno}: 相對 import 逸出 uep.stream")
        if rel.parts[0] != "adapters" and (hit := tops & SUBSYSTEM_NAMES):
            violations.append(f"{rel}: 非 adapters 不得 import 子系統 {sorted(hit)}")
    return violations


def test_uep_does_not_import_legacy():
    violations = []
    for path in sorted(UEP_ROOT.rglob("*.py")):
        hit = _imported_top_levels(path) & LEGACY_NAMES
        if hit:
            violations.append(f"{path.relative_to(UEP_ROOT.parent)}: {sorted(hit)}")
    assert not violations, "uep 不得 import legacy：\n" + "\n".join(violations)


def test_echo_stream_is_not_imported_anywhere_in_uep():
    violations = _retired_violations(UEP_ROOT)
    assert not violations, "uep/ 內禁止 import echo_stream（已併入 uep/stream）：\n" + "\n".join(violations)


def test_subsystems_only_imported_by_stream_adapters_and_wiring():
    violations = _subsystem_violations(UEP_ROOT)
    assert not violations, "子系統只允許 uep/stream/adapters/ 與 uep/echo/*_wiring.py import：\n" + "\n".join(violations)


def test_stream_is_one_way_and_pure_outside_adapters():
    assert STREAM_DIR.is_dir()
    violations = _stream_violations(STREAM_DIR)
    assert not violations, "uep/stream/ 邊界違規：\n" + "\n".join(violations)


def test_boundary_rules_catch_violations(tmp_path):
    """鎖住規則本身：違規要抓出，白名單與合法 import 要放行。"""
    root = tmp_path / "uep"
    for d in ("echo", "stream/adapters", "stream/audio", "memory"):
        (root / d).mkdir(parents=True)
    (root / "echo" / "x_wiring.py").write_text("from echo_memory import MemoryEngine\n", encoding="utf-8")
    (root / "echo" / "host.py").write_text("import echo_tts\nimport echo_stream.core\n", encoding="utf-8")
    (root / "memory" / "a.py").write_text("from echo_stream import X\nfrom llm_integration.tools import y\n", encoding="utf-8")
    (root / "stream" / "adapters" / "think.py").write_text(
        "import echo_thought_core\nfrom ..audio import vad\nfrom ...memory import z\n", encoding="utf-8")
    (root / "stream" / "audio" / "vad.py").write_text(
        "import echo_stt\nfrom uep.memory import q\nfrom ..splitter import ok\nfrom ... import bad\n", encoding="utf-8")
    (root / "stream" / "ok.py").write_text("from .audio import vad\nimport os\n", encoding="utf-8")

    assert [v.split(":")[0] for v in _retired_violations(root)] == [
        str(Path("echo") / "host.py"), str(Path("memory") / "a.py")]
    assert [v.split(":")[0] for v in _subsystem_violations(root)] == [
        str(Path("echo") / "host.py"), str(Path("memory") / "a.py"), str(Path("stream") / "audio" / "vad.py")]
    stream = _stream_violations(root / "stream")
    assert sorted(v.split(":")[0] for v in stream) == sorted([
        str(Path("adapters") / "think.py"),
        str(Path("audio") / "vad.py"), str(Path("audio") / "vad.py"), str(Path("audio") / "vad.py")])
    assert not any(v.startswith("ok.py") for v in stream)


def test_orchestrator_assembly_and_services_stay_out_of_stream_and_subsystems():
    """Orchestrator、組裝層與 services 不依賴 uep.stream 與子系統（沒有安裝推理依賴的環境也能組裝）。"""
    for rel in ("turn", "assembly.py", "echo/services.py", "echo/__init__.py", "echo/loop.py", "echo/worker.py"):
        target = UEP_ROOT / rel
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            modules = _imported_modules(path)
            tops = {m.split(".")[0] for m in modules}
            assert not (tops & (RETIRED_NAMES | SUBSYSTEM_NAMES)), f"{rel}：{path.name} 不應 import 子系統"
            assert not any(m == "uep.stream" or m.startswith("uep.stream.") for m in modules), (
                f"{rel}：{path.name} 不應 import uep.stream")


def _py_files(root: Path):
    """``root`` 底下的 .py 檔，略過虛擬環境、快取與隱藏目錄。"""
    for path in sorted(root.rglob("*.py")):
        rel_parts = path.relative_to(root).parts[:-1]
        if any(part in _SKIP_DIR_NAMES or part.startswith(".") or part.endswith(".egg-info") for part in rel_parts):
            continue
        yield path


def _forbidden_import_violations(root: Path, forbidden: set[str]) -> list[str]:
    """``root`` 底下 import 了 ``forbidden`` 內任一頂層名稱的檔案（無法解析的檔案略過：不會被執行就不會 import）。"""
    violations = []
    for path in _py_files(root):
        try:
            hit = _imported_top_levels(path) & forbidden
        except SyntaxError:
            continue
        if hit:
            violations.append(f"{path.relative_to(root)}: {sorted(hit)}")
    return violations


def test_subsystems_do_not_import_uep():
    """反向規則：subsystems/ 內（含各自的 tests/）不得 import uep。目錄不存在則直接通過。"""
    if not SUBSYSTEMS_ROOT.is_dir():
        return
    violations = _forbidden_import_violations(SUBSYSTEMS_ROOT, {"uep"})
    assert not violations, "subsystems/ 不得 import uep：\n" + "\n".join(violations)


def test_reverse_boundary_rule_catches_violations(tmp_path):
    """鎖住反向規則本身：違規要抓出、略過目錄與無法解析的檔案、缺目錄要容忍。"""
    sub = tmp_path / "subsystems"
    (sub / "echo_tts" / "tests").mkdir(parents=True)
    (sub / "echo_tts" / "env").mkdir()
    (sub / "echo_tts" / "tests" / "test_a.py").write_text("from uep.config import UepSettings\n", encoding="utf-8")
    (sub / "echo_tts" / "tests" / "test_b.py").write_text("import os\nimport echo_tts\n", encoding="utf-8")
    (sub / "echo_tts" / "env" / "vendored.py").write_text("import uep\n", encoding="utf-8")
    (sub / "echo_tts" / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert _forbidden_import_violations(sub, {"uep"}) == [str(Path("echo_tts") / "tests" / "test_a.py") + ": ['uep']"]
    assert _forbidden_import_violations(tmp_path / "missing", {"uep"}) == []


# ---- 決策層 <-> Agent 層白名單（DL-6a）----

_DECISION_MAY_IMPORT_AGENT = ("uep.agent.trigger",)
_AGENT_MAY_IMPORT_DECISION = ("uep.decision.loop_consult", "uep.decision.contracts")
_AGENT_REFERENCED_DECISION_MODULES = ("loop_consult.py", "contracts.py")


def _resolved_imports(path: Path, packages: tuple[str, ...]) -> set[str]:
    """絕對 import 的完整模組名；``from uep.agent import trigger`` 解析為 ``uep.agent.trigger``（``packages`` 內的套件才展開別名）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in packages or node.module == "uep":
                names.update(f"{node.module}.{alias.name}" for alias in node.names)
            else:
                names.add(node.module)
    return names


def _whitelist_violations(root: Path, target: str, allowed: tuple[str, ...]) -> list[str]:
    """``root`` 底下的檔案 import 了 ``target`` 套件但不在 ``allowed`` 白名單內的模組。"""
    violations = []
    for path in sorted(root.rglob("*.py")):
        for module in sorted(_resolved_imports(path, (target,))):
            in_target = module == target or module.startswith(target + ".")
            if in_target and not any(module == ok or module.startswith(ok + ".") for ok in allowed):
                violations.append(f"{path.relative_to(root)}: {module}")
    return violations


def test_decision_imports_agent_only_through_trigger():
    violations = _whitelist_violations(UEP_ROOT / "decision", "uep.agent", _DECISION_MAY_IMPORT_AGENT)
    assert not violations, "uep/decision 對 uep.agent 只准 import uep.agent.trigger：\n" + "\n".join(violations)


def test_agent_imports_decision_only_loop_consult_and_contracts():
    violations = _whitelist_violations(UEP_ROOT / "agent", "uep.decision", _AGENT_MAY_IMPORT_DECISION)
    assert not violations, (
        "uep/agent 對 uep.decision 只准 import loop_consult 與 contracts：\n" + "\n".join(violations))
    # 現況：loop.py 已經 import loop_consult，白名單必須放行
    assert "uep.decision.loop_consult" in _resolved_imports(UEP_ROOT / "agent" / "loop.py", ("uep.decision",))


def test_agent_referenced_decision_modules_do_not_pull_agent_except_trigger():
    for name in _AGENT_REFERENCED_DECISION_MODULES:
        modules = _resolved_imports(UEP_ROOT / "decision" / name, ("uep.agent",))
        agent = {m for m in modules if m == "uep.agent" or m.startswith("uep.agent.")}
        assert agent <= {"uep.agent.trigger"}, f"decision/{name} 不得 import uep.agent（trigger 除外）：{sorted(agent)}"


def test_whitelist_rules_catch_violations(tmp_path):
    """鎖住白名單規則本身：違規抓出，白名單與 ``from uep.agent import trigger`` 形式要放行。"""
    decision = tmp_path / "decision"
    agent = tmp_path / "agent"
    decision.mkdir()
    agent.mkdir()
    (decision / "ok_a.py").write_text(
        "from uep.agent.trigger import LoopTrigger\nfrom uep.agent import trigger\n", encoding="utf-8")
    (decision / "bad_a.py").write_text("from uep.agent.loop import AgentLoop\nimport uep.agent.core\n", encoding="utf-8")
    (decision / "bad_b.py").write_text("from uep.agent import loop\nfrom uep import agent\n", encoding="utf-8")
    (agent / "ok_b.py").write_text(
        "from uep.decision.loop_consult import x\nfrom uep.decision.contracts import y\n"
        "from uep.decision import contracts\n", encoding="utf-8")
    (agent / "bad_c.py").write_text(
        "from uep.decision.proactive import z\nfrom uep.decision import DecisionHub\nfrom uep import decision\n",
        encoding="utf-8")
    found = _whitelist_violations(decision, "uep.agent", _DECISION_MAY_IMPORT_AGENT)
    assert sorted({v.split(":")[0] for v in found}) == ["bad_a.py", "bad_b.py"]
    assert len(found) == 4
    found = _whitelist_violations(agent, "uep.decision", _AGENT_MAY_IMPORT_DECISION)
    assert sorted({v.split(":")[0] for v in found}) == ["bad_c.py"]
    assert len(found) == 3


# Module Coordinator（D108）：lifecycle 只認 contracts 與 events，不得反向依賴工具／決策／Echo／Agent／串流或子系統
_LIFECYCLE_FORBIDDEN = ("uep.tools", "uep.decision", "uep.echo", "uep.stream", "uep.agent")


def test_lifecycle_stays_independent_of_tools_decision_echo_stream_and_subsystems():
    root = UEP_ROOT / "lifecycle"
    assert root.is_dir()
    for target in _LIFECYCLE_FORBIDDEN:
        violations = _whitelist_violations(root, target, ())
        assert not violations, f"uep/lifecycle 不得 import {target}：\n" + "\n".join(violations)
    for path in sorted(root.rglob("*.py")):
        hit = _imported_top_levels(path) & (SUBSYSTEM_NAMES | LEGACY_NAMES | RETIRED_NAMES)
        assert not hit, f"uep/lifecycle/{path.name} 不得 import 子系統／legacy：{sorted(hit)}"


def test_provision_contract_depends_on_nothing_in_uep():
    modules = {m for m in _resolved_imports(UEP_ROOT / "contracts" / "provision.py", ("uep",)) if m.split(".")[0] == "uep"}
    assert not modules, f"uep/contracts/provision.py 必須零依賴：{sorted(modules)}"


def test_lifecycle_boundary_rule_catches_violations(tmp_path):
    (tmp_path / "bad.py").write_text("from uep.tools.registry import ToolRegistry\nimport uep.decision\n", encoding="utf-8")
    (tmp_path / "ok.py").write_text("from uep.contracts.provision import ProvisionItem\nfrom uep.events import bus\n", encoding="utf-8")
    found = _whitelist_violations(tmp_path, "uep.tools", ()) + _whitelist_violations(tmp_path, "uep.decision", ())
    assert sorted({v.split(":")[0] for v in found}) == ["bad.py"] and len(found) == 2
