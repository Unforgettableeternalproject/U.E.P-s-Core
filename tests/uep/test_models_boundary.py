"""模型層 SDK 邊界（Agent 架構提案 §7.2-2）：PydanticAI 的 import 只允許出現在 ``uep/models/``。"""
import ast
from pathlib import Path

UEP_ROOT = Path(__file__).resolve().parents[2] / "uep"
MODELS_DIR = UEP_ROOT / "models"
SDK_NAMES = {"pydantic_ai", "pydantic_graph"}


def _sdk_imports(source: str) -> set[str]:
    tree = ast.parse(source)
    hit: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            hit.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            hit.add(node.module.split(".")[0])
    return hit & SDK_NAMES


def _violations(root: Path) -> list[str]:
    return [
        str(p.relative_to(root))
        for p in sorted(root.rglob("*.py"))
        if MODELS_DIR not in p.parents and _sdk_imports(p.read_text(encoding="utf-8"))
    ]


def test_sdk_is_imported_only_inside_uep_models():
    assert _violations(UEP_ROOT) == []


def test_detector_catches_violations():
    assert _sdk_imports("import pydantic_ai") == {"pydantic_ai"}
    assert _sdk_imports("from pydantic_ai.models import Model") == {"pydantic_ai"}
    assert _sdk_imports("from pydantic_graph import End") == {"pydantic_graph"}
    assert _sdk_imports("import json\nfrom uep.models import ModelPort") == set()


def test_models_package_is_the_one_place_that_uses_it():
    # 防止偵測器空轉：uep/models 內確實有 import（否則整個測試沒有意義）
    assert any(_sdk_imports(p.read_text(encoding="utf-8")) for p in MODELS_DIR.glob("*.py"))
