"""uep 套件與 legacy 的 import 邊界檢查（N1 後；D104／D105）。

- legacy（core／modules／utils／devtools／Entry）一律禁止。
- ``echo_stream`` 在 ``uep/`` 內全面禁止：ES 的契約與核心已併入 ``uep/stream/``（N1），ES repo 轉為 bot 維護線。
- 子系統（``echo_memory``、``echo_stt``、``echo_thought_core``、``echo_tts``、``llm_integration``、``mcp_server``）只允許
  ``uep/stream/adapters/`` 與 ``uep/echo/*_wiring.py`` import；其餘模組只認 Protocol 與 fake。
- ``uep/stream/`` 單向依賴：不得 import ``uep`` 其他部分（絕對與逸出本套件的相對 import 都算）；除 ``adapters/`` 外不得 import 子系統。
- Orchestrator、組裝層與 services 不依賴 ``uep.stream``（沒有安裝推理依賴的環境也能組裝）。
- 反向規則（子系統遷入 Core，ADR-M1）：``subsystems/`` 內不得 import ``uep``。
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
