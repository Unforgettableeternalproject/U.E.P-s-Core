"""uep 套件與 legacy 的 import 邊界檢查。"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
# echo_memory／echo_stream 是外部函式庫：uep 只用 Protocol 與 fake，不得硬依賴（真 engine 需 3.12 環境，S6 才接）
LEGACY_NAMES = {"core", "modules", "utils", "devtools", "Entry", "echo_memory", "echo_stream"}


def _imported_top_levels(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_uep_does_not_import_legacy():
    violations = []
    for path in sorted(UEP_ROOT.rglob("*.py")):
        hit = _imported_top_levels(path) & LEGACY_NAMES
        if hit:
            violations.append(f"{path.relative_to(UEP_ROOT.parent)}: {sorted(hit)}")
    assert not violations, "uep 不得 import legacy：\n" + "\n".join(violations)
