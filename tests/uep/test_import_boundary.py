"""uep 套件與 legacy 的 import 邊界檢查。

- legacy（core／modules／utils／devtools／Entry）一律禁止。
- ``echo_stream`` 與 ``echo_memory``（依賴方向 ``uep -> 外部函式庫``）只有 ``uep/echo/`` 可以 import（S6 起記憶端真實接線
  放在 ``uep/echo/memory_wiring.py``）；其他模組仍禁止，只認 ``uep/memory/protocol.py`` 的 Protocol 與 fake。
- 反向規則（子系統遷入 Core，ADR-M1）：``subsystems/`` 內不得 import ``uep``；ES 的 ``contracts/``、``core/`` 不得 import
  其他子系統。遷入前 ``subsystems/`` 不存在，這兩條規則直接通過，並以假目錄測試鎖住規則本身。
"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
ECHO_DIR = UEP_ROOT / "echo"
LEGACY_NAMES = {"core", "modules", "utils", "devtools", "Entry"}
# 只有 uep/echo/ 轉接層可以 import 的外部函式庫（echo_memory 是外部函式庫：uep 其餘部分只用 Protocol 與 fake）
ECHO_ONLY_NAMES = {"echo_stream", "echo_memory"}

# 反向規則用：子系統目錄（遷入前不存在）與各子系統的頂層套件名
SUBSYSTEMS_ROOT = UEP_ROOT.parent / "subsystems"
ES_PURE_DIRS = ("contracts", "core")
# ES 以外的子系統套件：ES 的 contracts/、core/ 不得 import（只有 adapters/ 可以）
ES_FORBIDDEN_NAMES = {"echo_memory", "echo_stt", "echo_thought_core", "echo_tts", "llm_integration", "mcp_server"}
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


def _is_in_echo(path: Path) -> bool:
    return ECHO_DIR in path.parents


def _echo_stream_violations(root: Path, echo_dir: Path) -> list[str]:
    """``root`` 底下、``echo_dir`` 以外 import 了 echo_stream 的檔案（邊界規則本身，可用假目錄驗證）。"""
    violations = []
    for path in sorted(root.rglob("*.py")):
        if echo_dir in path.parents:
            continue
        hit = _imported_top_levels(path) & ECHO_ONLY_NAMES
        if hit:
            violations.append(f"{path.relative_to(root)}: {sorted(hit)}")
    return violations


def test_uep_does_not_import_legacy():
    violations = []
    for path in sorted(UEP_ROOT.rglob("*.py")):
        hit = _imported_top_levels(path) & LEGACY_NAMES
        if hit:
            violations.append(f"{path.relative_to(UEP_ROOT.parent)}: {sorted(hit)}")
    assert not violations, "uep 不得 import legacy：\n" + "\n".join(violations)


def test_echo_stream_only_imported_by_uep_echo():
    violations = _echo_stream_violations(UEP_ROOT, ECHO_DIR)
    assert not violations, "只有 uep/echo/ 可以 import echo_stream：\n" + "\n".join(violations)


def test_boundary_rule_catches_violations_outside_echo(tmp_path):
    """鎖住規則本身：echo/ 內可以、echo/ 外（含 import／from 兩種形式、子模組）一律抓出。"""
    root = tmp_path / "pkg"
    (root / "echo").mkdir(parents=True)
    (root / "other").mkdir()
    (root / "echo" / "ok.py").write_text("import echo_stream\nfrom echo_stream.core import x\n", encoding="utf-8")
    (root / "other" / "a.py").write_text("import echo_stream.contracts\n", encoding="utf-8")
    (root / "other" / "b.py").write_text("from echo_stream import TurnCoordinator\n", encoding="utf-8")
    (root / "other" / "c.py").write_text("import os\nfrom uep.echo.host import EchoPipelineHost\n", encoding="utf-8")
    found = _echo_stream_violations(root, root / "echo")
    assert [v.split(":")[0] for v in found] == [str(Path("other") / "a.py"), str(Path("other") / "b.py")]


def test_echo_stream_stays_out_of_turn_orchestrator_and_assembly():
    """Orchestrator、組裝層與 services 不依賴 echo_stream（沒有安裝 ES 的環境也能組裝）。"""
    for rel in ("turn", "assembly.py", "echo/services.py", "echo/__init__.py", "echo/loop.py", "echo/worker.py"):
        target = UEP_ROOT / rel
        files = sorted(target.rglob("*.py")) if target.is_dir() else [target]
        for path in files:
            assert not (_imported_top_levels(path) & ECHO_ONLY_NAMES), f"{rel}：{path.name} 不應 import echo_stream"


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


def _es_pure_violations(es_package_root: Path) -> list[str]:
    """ES 套件根目錄下 ``contracts/``、``core/`` 對其他子系統的 import（目錄不存在視為沒有違規）。"""
    violations = []
    for name in ES_PURE_DIRS:
        target = es_package_root / name
        if target.is_dir():
            violations += [f"{name}/{v}" for v in _forbidden_import_violations(target, ES_FORBIDDEN_NAMES)]
    return violations


def test_subsystems_do_not_import_uep():
    """反向規則：subsystems/ 內（含各自的 tests/）不得 import uep。遷入前目錄不存在，直接通過。"""
    if not SUBSYSTEMS_ROOT.is_dir():
        return
    violations = _forbidden_import_violations(SUBSYSTEMS_ROOT, {"uep"})
    assert not violations, "subsystems/ 不得 import uep：\n" + "\n".join(violations)


def test_es_contracts_and_core_do_not_import_other_subsystems():
    """反向規則：echo_stream 的 contracts/、core/ 不得 import 其他子系統（只有 adapters/ 可以）。遷入前目錄不存在，直接通過。"""
    es_root = SUBSYSTEMS_ROOT / "echo_stream" / "echo_stream"
    if not es_root.is_dir():
        return
    violations = _es_pure_violations(es_root)
    assert not violations, "ES contracts/、core/ 不得 import 其他子系統：\n" + "\n".join(violations)


def test_reverse_boundary_rules_catch_violations(tmp_path):
    """鎖住反向規則本身（遷入前沒有真實目錄可檢查）：違規要抓出、adapters／合法 import 要放行、缺目錄要容忍。"""
    sub = tmp_path / "subsystems"
    (sub / "echo_tts" / "tests").mkdir(parents=True)
    (sub / "echo_tts" / "env").mkdir()
    (sub / "echo_tts" / "tests" / "test_a.py").write_text("from uep.config import UepSettings\n", encoding="utf-8")
    (sub / "echo_tts" / "tests" / "test_b.py").write_text("import os\nimport echo_tts\n", encoding="utf-8")
    (sub / "echo_tts" / "env" / "vendored.py").write_text("import uep\n", encoding="utf-8")
    (sub / "echo_tts" / "broken.py").write_text("def (:\n", encoding="utf-8")
    assert _forbidden_import_violations(sub, {"uep"}) == [str(Path("echo_tts") / "tests" / "test_a.py") + ": ['uep']"]

    es = tmp_path / "echo_stream"
    for d in ("contracts", "core", "adapters"):
        (es / d).mkdir(parents=True)
    (es / "contracts" / "ok.py").write_text("from echo_stream.core import x\nimport pydantic\n", encoding="utf-8")
    (es / "core" / "bad.py").write_text("import echo_memory\nfrom echo_tts.runtime import y\n", encoding="utf-8")
    (es / "adapters" / "fine.py").write_text("import echo_stt\nimport echo_tts\n", encoding="utf-8")
    assert _es_pure_violations(es) == [f"core/{Path('bad.py')}: ['echo_memory', 'echo_tts']"]
    assert _es_pure_violations(tmp_path / "missing") == []
    assert _forbidden_import_violations(es / "contracts", ES_FORBIDDEN_NAMES) == []
