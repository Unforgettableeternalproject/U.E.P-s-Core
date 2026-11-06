"""切片 D1b（離線）：TC-3 的 Run 內並行放寬——規則函式、resolver 的 concurrency 投影、FakeTasks 守衛、欄位不可改。

DB 端對照（真實 Manager、兩個執行緒搶 EXECUTING、0012 往返）在 ``test_tc3_parallel_db.py``。
"""
from __future__ import annotations

import ast
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.uep.loop_support import FakeTasks, make_registry
from uep.fakes.tool_spec import FakeToolSpecResolver
from uep.tasks.manager import ActionBlocked, TaskManager, executing_set_allowed
from uep.tools.registry import StaticToolRegistry

EPOCH = 1


# ---- 規則函式 ----

@pytest.mark.parametrize(
    ("incoming", "executing", "allowed"),
    [
        ("serial", [], True),
        ("parallel_safe", [], True),
        ("parallel_one_shot", [], True),
        ("parallel_safe", ["parallel_safe"], True),
        ("parallel_safe", ["parallel_safe", "parallel_safe"], True),
        ("serial", ["parallel_safe"], False),
        ("parallel_safe", ["serial"], False),
        ("serial", ["serial"], False),
        ("parallel_one_shot", ["parallel_safe"], False),  # 本期視同 serial
        ("parallel_safe", ["parallel_one_shot"], False),
        ("parallel_one_shot", ["parallel_one_shot"], False),
        ("bogus", [], True),
        ("bogus", ["parallel_safe"], False),  # 未知值視同 serial
        ("parallel_safe", ["bogus"], False),
    ],
)
def test_executing_set_allowed_truth_table(incoming, executing, allowed):
    assert executing_set_allowed(incoming, executing) is allowed


# ---- resolver 的 concurrency 投影（create_action 的來源）----

def _resolve(resolver, tool):
    return TaskManager._resolve_concurrency(SimpleNamespace(_tool_resolver=resolver), tool)


def test_registry_projects_declared_concurrency():
    registry = make_registry()
    assert _resolve(registry, "read.a") == "parallel_safe"
    assert _resolve(registry, "read.lone") == "serial"
    assert _resolve(registry, "write.s") == "serial"


def test_fake_resolver_projects_concurrency_and_defaults_to_serial():
    resolver = FakeToolSpecResolver(**{"x.safe": {"concurrency": "parallel_safe"}, "x.plain": {}})
    assert _resolve(resolver, "x.safe") == "parallel_safe"
    assert _resolve(resolver, "x.plain") == "serial"


def test_concurrency_is_fail_closed():
    registry = make_registry()
    assert _resolve(registry, "no.such.tool") == "serial"  # 解析不到
    static = StaticToolRegistry({"a.b": registry.resolve("read.a")})
    assert _resolve(static, "a.b") == "serial"  # resolver 不支援 concurrency_of

    class Broken:
        def concurrency_of(self, tool):
            raise RuntimeError("boom")

    class Garbage:
        def concurrency_of(self, tool):
            return "parallel_everything"

    assert _resolve(Broken(), "read.a") == "serial"
    assert _resolve(Garbage(), "read.a") == "serial"  # 非法值


def test_concurrency_is_chosen_by_declaration_not_tool_name():
    """TG-1：不看工具名稱；名稱像 read 的 serial 工具仍是 serial，名稱像 write 的 parallel_safe 工具照宣告。"""
    resolver = FakeToolSpecResolver(**{"read.everything": {}, "write.nothing": {"concurrency": "parallel_safe"}})
    assert _resolve(resolver, "read.everything") == "serial"
    assert _resolve(resolver, "write.nothing") == "parallel_safe"


# ---- concurrency 建立後不可改（程式結構證明）----

def test_no_code_path_in_tasks_package_writes_action_concurrency():
    """uep/tasks 內唯一寫 ``concurrency`` 的地方是 create_action 建構 ActionIntentRow 時的關鍵字參數；
    沒有任何屬性賦值（``x.concurrency = ...``）、沒有 update(...).values(concurrency=...)，更新 action 列只經
    ``_set_action_status``（status／version）與 approval_ref。"""
    root = Path(__file__).resolve().parents[2] / "uep" / "tasks"
    for path in root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                assert not (isinstance(target, ast.Attribute) and target.attr == "concurrency"), (path.name, node.lineno)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("values", "update"):
                assert all(kw.arg != "concurrency" for kw in node.keywords), (path.name, node.lineno)
    manager_calls = [
        node for node in ast.walk(ast.parse((root / "manager.py").read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and any(kw.arg == "concurrency" for kw in node.keywords)
    ]
    assert len(manager_calls) == 1  # 只有 create_action 的 ActionIntentRow(...) 建構


def test_manager_has_no_public_method_to_change_concurrency():
    names = [n for n in dir(TaskManager) if "concurrency" in n.lower()]
    assert names == ["_resolve_concurrency"]  # 只讀 resolver，不寫列


# ---- FakeTasks 守衛（與真實 _check_single_executing 同一規則）----

def _tasks(**behaviors):
    return FakeTasks(make_registry(), behaviors)


def _action(tasks, action_id, tool, path="p"):
    tasks.create_action(run_id="run_1", step_id="s", tool=tool, params={"path": path}, worker_epoch=EPOCH, action_id=action_id)


class Held:
    """讓某個工具的 behavior 卡住，直到 release；用來在 EXECUTING 期間做第二次呼叫。"""

    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def __call__(self, args):
        self.started.set()
        assert self.release.wait(5)
        return "held"


def _hold(tasks, action_id):
    result = {}

    def target():
        try:
            result["outcome"] = tasks.execute_action(action_id, {"path": "p"}, worker_epoch=EPOCH)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    return thread, result


def test_fake_two_parallel_safe_execute_together():
    held = Held()
    tasks = _tasks(**{"read.a": held})
    _action(tasks, "act_1", "read.a")
    _action(tasks, "act_2", "read.b", "q")
    thread, result = _hold(tasks, "act_1")
    assert held.started.wait(5)
    assert tasks.execute_action("act_2", {"path": "q"}, worker_epoch=EPOCH).executed  # act_1 仍 EXECUTING
    held.release.set()
    thread.join()
    assert "error" not in result


@pytest.mark.parametrize(("first", "second"), [
    ("read.a", "read.lone"),  # parallel_safe 進行中，serial 不能進
    ("read.lone", "read.a"),  # serial 進行中，parallel_safe 不能進
    ("write.s", "read.lone"),  # serial 與 serial 互斥
])
def test_fake_serial_excludes_everything_else(first, second):
    held = Held()
    tasks = _tasks(**{first: held})
    _action(tasks, "act_1", first)
    _action(tasks, "act_2", second, "q")
    thread, result = _hold(tasks, "act_1")
    assert held.started.wait(5)
    with pytest.raises(ActionBlocked, match="TC-3"):
        tasks.execute_action("act_2", {"path": "q"}, worker_epoch=EPOCH)
    held.release.set()
    thread.join()
    assert "error" not in result
    assert tasks.execute_action("act_2", {"path": "q"}, worker_epoch=EPOCH).executed  # 收尾後即可進入


def test_fake_non_read_unknown_blocks_but_internal_read_unknown_does_not():
    def lost(args):
        raise RuntimeError("lost")

    tasks = _tasks(**{"write.s": lost})
    _action(tasks, "act_w", "write.s")
    _action(tasks, "act_n", "read.a", "n")
    with pytest.raises(RuntimeError):
        tasks.execute_action("act_w", {"path": "p"}, worker_epoch=EPOCH)
    with pytest.raises(ActionBlocked, match="TC-3"):
        tasks.execute_action("act_n", {"path": "n"}, worker_epoch=EPOCH)

    tasks2 = _tasks(**{"read.lone": lost})
    _action(tasks2, "act_r", "read.lone")
    _action(tasks2, "act_n", "read.a", "n")
    with pytest.raises(RuntimeError):
        tasks2.execute_action("act_r", {"path": "p"}, worker_epoch=EPOCH)
    assert tasks2.execute_action("act_n", {"path": "n"}, worker_epoch=EPOCH).executed
