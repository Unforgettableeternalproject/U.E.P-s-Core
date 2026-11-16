"""/restart 與 /flags：參數解析、覆寫檔累加與清除、退出碼、旗標輸出（不需 DB，以最小 stub 驅動指令層）。"""
from __future__ import annotations

import pytest

from uep.config import get_settings
from uep.console import app
from uep.console import restart as restart_mod
from uep.console.commands import Console
from uep.console.restart import RESTART_EXIT_CODE, RestartError


class Capture:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def override_file(monkeypatch, tmp_path):
    path = tmp_path / "outputs" / "e2e" / "console_overrides.env"
    monkeypatch.setenv(restart_mod.OVERRIDE_FILE_ENV, str(path))
    monkeypatch.delenv(restart_mod.RELAUNCH_ENV, raising=False)
    return path


@pytest.fixture
def console_and_out(override_file):
    out = Capture()
    console = Console(None, out=out)  # type: ignore[arg-type]  # /restart、/flags 不碰 runtime
    return console, out


# ---- 參數解析 ----

def test_parse_多組鍵值與空值():
    assert restart_mod.parse_overrides("UEP_BEHAVIOR_DRIVER=on UEP_SCHEDULE_REST_WINDOW=plan UEP_TOOL_TESTKIT=") == {
        "UEP_BEHAVIOR_DRIVER": "on", "UEP_SCHEDULE_REST_WINDOW": "plan", "UEP_TOOL_TESTKIT": "",
    }
    assert restart_mod.parse_overrides("") == {}


@pytest.mark.parametrize("arg", ["PATH=x", "uep_x=1", "FOO_BAR=1", "UEP_=1x y"])
def test_parse_拒絕非UEP鍵(arg):
    with pytest.raises(RestartError):
        restart_mod.parse_overrides(arg)


def test_parse_拒絕缺等號與bat不安全字元():
    with pytest.raises(RestartError):
        restart_mod.parse_overrides("UEP_X")
    for bad in ('UEP_X=a"b', "UEP_X=a%b", "UEP_X=a&b", "UEP_X=a|b", "UEP_X=a^b"):
        with pytest.raises(RestartError):
            restart_mod.parse_overrides(bad)


# ---- 覆寫檔累加與清除 ----

def test_merge_累加覆蓋與空值清除():
    merged = restart_mod.merge_overrides({"UEP_A": "1", "UEP_B": "2"}, {"UEP_B": "9", "UEP_C": "3", "UEP_A": ""})
    assert merged == {"UEP_B": "9", "UEP_C": "3"}


def test_覆寫檔寫入讀回(override_file):
    assert restart_mod.read_overrides() == {}
    restart_mod.write_overrides({"UEP_B": "2", "UEP_A": "1"})
    assert override_file.read_text(encoding="utf-8").splitlines() == ["UEP_A=1", "UEP_B=2"]
    assert restart_mod.read_overrides() == {"UEP_A": "1", "UEP_B": "2"}


# ---- /restart 指令 ----

def test_restart_寫檔並要求結束且退出碼為75(console_and_out, override_file):
    console, out = console_and_out
    assert console.execute("/restart UEP_BEHAVIOR_DRIVER=on UEP_SCHEDULE_REST_WINDOW=plan") is False
    assert console.exit_code == RESTART_EXIT_CODE == 75
    assert restart_mod.read_overrides() == {"UEP_BEHAVIOR_DRIVER": "on", "UEP_SCHEDULE_REST_WINDOW": "plan"}
    assert "75" in out.text and "不會自動重啟" in out.text  # 沒有 bat 設的 RELAUNCH 旗標時提醒


def test_restart_由bat啟動時不提醒(console_and_out, monkeypatch):
    console, out = console_and_out
    monkeypatch.setenv(restart_mod.RELAUNCH_ENV, "1")
    assert console.execute("/restart UEP_X=1") is False
    assert "不會自動重啟" not in out.text


def test_restart_同一次bat內累加且空值還原(override_file):
    first = Console(None, out=Capture())  # type: ignore[arg-type]
    first.execute("/restart UEP_BEHAVIOR_DRIVER=on UEP_TOOL_TESTKIT=1")
    second = Console(None, out=Capture())  # type: ignore[arg-type]  # 模擬重啟後的新行程
    second.execute("/restart UEP_BEHAVIOR_DRIVER= UEP_SCHEDULE_REST_WINDOW=plan")
    assert restart_mod.read_overrides() == {"UEP_TOOL_TESTKIT": "1", "UEP_SCHEDULE_REST_WINDOW": "plan"}


def test_restart_不帶參數保留既有覆寫並重啟(override_file):
    restart_mod.write_overrides({"UEP_TOOL_TESTKIT": "1"})
    console = Console(None, out=Capture())  # type: ignore[arg-type]
    assert console.execute("/restart") is False
    assert console.exit_code == RESTART_EXIT_CODE
    assert restart_mod.read_overrides() == {"UEP_TOOL_TESTKIT": "1"}


def test_restart_拒絕非UEP鍵不寫檔也不結束(console_and_out, override_file):
    console, out = console_and_out
    assert console.execute("/restart PATH=x UEP_A=1") is True
    assert console.exit_code == 0
    assert "只接受 UEP_ 開頭" in out.text
    assert not override_file.exists()


def test_其他指令不影響迴圈繼續(console_and_out):
    console, _ = console_and_out
    assert console.execute("/help") is True
    assert console.exit_code == 0


# ---- main 的退出碼 ----

def test_main_把restart退出碼當行程退出碼(monkeypatch, tmp_path, override_file):
    class StubRuntime:
        mode = "stub"
        warmups = {}  # noqa: RUF012
        memory_root = None
        session_dir = tmp_path
        closed = False

        def close(self) -> None:
            self.closed = True

    runtime = StubRuntime()
    monkeypatch.setattr(app, "connect_database", lambda: object())
    monkeypatch.setattr(app, "build_runtime", lambda *a, **k: runtime)
    lines = iter(["/restart UEP_X=1"])
    out = Capture()
    assert app.main(["--fake"], input_fn=lambda _p: next(lines), out=out) == RESTART_EXIT_CODE
    assert runtime.closed is True  # 照現有退出路徑正常關閉

    lines2 = iter(["/quit"])
    assert app.main(["--fake"], input_fn=lambda _p: next(lines2), out=out) == 0


# ---- /flags ----

def test_flags_列出生效值並標出覆寫(console_and_out, monkeypatch):
    console, out = console_and_out
    settings = get_settings()
    monkeypatch.setattr(settings, "tool_path", "agent")
    monkeypatch.setattr(settings, "agent_loop_entry", True)
    monkeypatch.setattr(settings, "schedule_behavior_driver", "shadow")
    monkeypatch.setattr(settings, "dream_window_start", None)
    restart_mod.write_overrides({"UEP_BEHAVIOR_DRIVER": "on", "UEP_ZZZ_CUSTOM": "1"})
    assert console.execute("/flags") is True
    lines = {line.split(" = ")[0].strip("* ").strip(): line for line in out.lines if " = " in line}
    assert "UEP_TOOL_PATH = agent" in lines["UEP_TOOL_PATH"]
    assert "UEP_AGENT_LOOP_ENTRY = on" in lines["UEP_AGENT_LOOP_ENTRY"]
    assert "（未設）" in lines["UEP_DREAM_WINDOW_START"]
    assert lines["UEP_BEHAVIOR_DRIVER"].startswith("*")  # 被覆寫
    assert not lines["UEP_TOOL_PATH"].startswith("*")
    # 顯示的是 settings 的值（此處 shadow），不是覆寫檔字串；覆寫要下次重啟才生效
    assert "UEP_BEHAVIOR_DRIVER = shadow" in lines["UEP_BEHAVIOR_DRIVER"]
    assert "UEP_ZZZ_CUSTOM=1" in out.text
    assert console.exit_code == 0


def test_flags_涵蓋全部測試旗標且皆對得到settings欄位():
    rows = restart_mod.effective_flags(get_settings())
    assert [r[0] for r in rows] == [k for k, _ in restart_mod.TEST_FLAGS]
    assert all(r[1] != "（無此欄位）" for r in rows)
