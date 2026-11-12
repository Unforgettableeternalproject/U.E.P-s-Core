"""VC-PP-8 / PP-3：行程模組的 import 邊界（靜態）。"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
FORBIDDEN_PREFIXES = ("uep.memory", "uep.presence", "uep.state", "uep.decision", "uep.agent", "uep.stream")


def _modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            if node.module == "uep":
                names.update(f"uep.{a.name}" for a in node.names)
    return names


def test_schedule_package_only_depends_on_contracts():
    violations = []
    for path in sorted((UEP_ROOT / "schedule").rglob("*.py")):
        for m in _modules(path):
            if m.split(".")[0] != "uep":
                continue
            if m.startswith(FORBIDDEN_PREFIXES) or not m.startswith(("uep.contracts", "uep.schedule")):
                violations.append(f"{path.name}: {m}")
    assert not violations, "uep/schedule 只能依賴 uep.contracts（PP-3）：\n" + "\n".join(violations)


def test_schedule_contract_is_zero_dependency():
    assert not [m for m in _modules(UEP_ROOT / "contracts" / "schedule.py") if m.split(".")[0] == "uep"]
