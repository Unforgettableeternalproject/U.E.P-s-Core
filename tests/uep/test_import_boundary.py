"""uep 套件與 legacy 的 import 邊界檢查。

- legacy（core／modules／utils／devtools／Entry）與 echo_memory 一律禁止（真 engine 需 3.12 環境，S6 才接）。
- ``echo_stream``（依賴方向 ``uep -> echo_stream``）只有 ``uep/echo/`` 可以 import；其他模組仍禁止。
"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
ECHO_DIR = UEP_ROOT / "echo"
# echo_memory 是外部函式庫：uep 只用 Protocol 與 fake，不得硬依賴
LEGACY_NAMES = {"core", "modules", "utils", "devtools", "Entry", "echo_memory"}
# 只有 uep/echo/ 轉接層可以 import 的外部函式庫
ECHO_ONLY_NAMES = {"echo_stream"}


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
