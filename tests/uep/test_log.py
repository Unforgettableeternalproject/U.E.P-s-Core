"""uep.log：位置標註、子系統推導、等級解析、冪等、contextvars、遮罩、JSONL。

只用獨立的 logger 與自掛的 handler；``configure_logging`` 的測試在結束時 ``shutdown_logging`` 並還原 root，
不在 pytest 行程留下 root handler。
"""
from __future__ import annotations

import io
import json
import logging
import threading
from types import SimpleNamespace

import pytest

from uep import log as uplog


@pytest.fixture
def capture():
    """獨立 logger ``uep.logtest``＋StringIO handler（掛與 configure_logging 相同的 filter／formatter）。"""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(uplog.UepFormatter(with_date=False))
    handler.addFilter(uplog._CaptureFilter())
    logger = logging.getLogger("uep.logtest")
    logger.addHandler(handler)
    logger.setLevel(uplog.TRACE)
    logger.propagate = False
    yield logger, stream, handler
    logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    before = (list(root.handlers), root.level)
    yield root
    uplog.shutdown_logging()
    root.handlers[:] = before[0]
    root.setLevel(before[1])


def test_where_has_relative_path_and_lineno(capture):
    logger, stream, _ = capture
    logger.info("hello")
    lineno = test_where_has_relative_path_and_lineno.__code__.co_firstlineno + 2
    out = stream.getvalue()
    assert f"tests/uep/test_log.py:{lineno} test_where_has_relative_path_and_lineno | hello" in out
    assert "[core.logtest]" in out


def test_get_logger_default_name_and_key_trace_locations(capture):
    _, stream, _ = capture
    lg = uplog.get_logger("uep.logtest")
    lg.key("k")
    lg.trace("t")
    out = stream.getvalue()
    assert " KEY " in out and " TRACE " in out
    # key()／trace() 的位置要指向本檔呼叫行，不是 uep/log.py
    assert "uep/log.py" not in out
    assert out.count("tests/uep/test_log.py") == 2
    assert uplog.get_logger().logger.name == __name__


def test_third_party_falls_back_to_name(capture):
    _, stream, handler = capture
    record = logging.LogRecord("httpx", logging.INFO, "/site-packages/httpx/_client.py", 10, "x", (), None, "send")
    handler.handle(record)
    assert "[httpx] httpx | x" in stream.getvalue()


@pytest.mark.parametrize(
    ("name", "rel", "expected"),
    [
        ("uep.tasks.manager", "uep/tasks/manager.py", "core.tasks"),
        ("uep", None, "core"),
        ("echo_tts", "subsystems/echo_tts/echo_tts/utils/log_helper.py", "echo_tts"),
        ("echo_memory.mcp", None, "echo_memory"),
        ("entry", "subsystems/echo_memory/Entry.py", "echo_memory"),
        ("__main__", "uep/console/__main__.py", "core.console"),
        ("httpx", None, "httpx"),
        ("urllib3.connectionpool", None, "urllib3"),
    ],
)
def test_derive_subsystem(name, rel, expected):
    assert uplog.derive_subsystem(name, rel) == expected


def test_parse_levels_valid_and_malformed():
    valid, bad = uplog.parse_levels("uep.decision=DEBUG, echo_tts=warning,oops,=INFO,x=NOPE,uep.k=KEY")
    assert valid == {"uep.decision": logging.DEBUG, "echo_tts": logging.WARNING, "uep.k": uplog.KEY}
    assert bad == ["oops", "=INFO", "x=NOPE"]
    assert uplog.parse_levels(None) == ({}, [])


def test_depth_mapping():
    assert [uplog.depth_to_level(d) for d in (1, 2, 3, 4)] == [uplog.KEY, logging.INFO, logging.DEBUG, uplog.TRACE]
    assert uplog.depth_to_level(0) is None and uplog.depth_to_level(5) is None
    assert logging.INFO < uplog.KEY < logging.WARNING and uplog.TRACE < logging.DEBUG


def test_configure_idempotent_and_depth(clean_root, tmp_path):
    settings = SimpleNamespace(log_depth="4", log_dir=tmp_path, log_levels="uep.decision=DEBUG,bad", log_json=True)
    uplog.configure_logging(settings)
    uplog.configure_logging(settings)
    ours = [h for h in clean_root.handlers if uplog._is_ours(h)]
    assert len(ours) == 2  # console＋queue，重複呼叫不增加
    assert clean_root.level == uplog.TRACE
    assert logging.getLogger("uep.decision").level == logging.DEBUG
    assert logging.getLogger("httpx").level == logging.WARNING
    uplog.shutdown_logging()
    assert not [h for h in clean_root.handlers if uplog._is_ours(h)]
    assert logging.getLogger("uep.decision").level == logging.NOTSET


def test_level_overrides_depth_and_invalid_warns_once(clean_root, tmp_path, capsys):
    uplog.configure_logging(SimpleNamespace(log_level="DEBUG", log_depth="1", log_dir=tmp_path, log_levels="a=1x,b", log_file=False))
    assert clean_root.level == logging.DEBUG
    err = capsys.readouterr().err
    assert err.count("UEP_LOG_LEVELS") == 1 and "a=1x" in err and "b" in err


def test_files_written_per_process_with_json_fields(clean_root, tmp_path):
    uplog.configure_logging(SimpleNamespace(log_level="INFO", log_dir=tmp_path, log_json=True))
    with uplog.log_context(run_id="run_1", turn_id="t_9"):
        logging.getLogger("uep.logtest").info("api_key=abc123 完成")
    logging.getLogger("uep.logtest").error("boom")
    uplog.shutdown_logging()  # 停 listener 並 flush
    names = sorted(p.name for p in tmp_path.iterdir())
    assert any(n.endswith(".error.log") for n in names) and any(n.endswith(".jsonl") for n in names)
    jsonl = next(p for p in tmp_path.iterdir() if p.suffix == ".jsonl")
    rows = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert {"ts", "level", "subsystem", "logger", "where", "msg", "context"} <= set(rows[0])
    assert rows[0]["context"] == {"run_id": "run_1", "turn_id": "t_9"}
    assert rows[0]["msg"] == "api_key=*** 完成"
    assert rows[0]["subsystem"] == "core.logtest" and rows[0]["where"].startswith("tests/uep/test_log.py:")
    assert rows[1]["context"] == {}
    err_text = next(p for p in tmp_path.iterdir() if p.name.endswith(".error.log")).read_text(encoding="utf-8")
    assert "boom" in err_text and "完成" not in err_text


def test_context_injected_nested_and_not_leaked_across_threads(capture):
    logger, stream, _ = capture
    with uplog.log_context(run_id="r1"):
        with uplog.log_context(turn_id="t1", request_id=None):
            logger.info("inner")
        logger.info("outer")
        worker = threading.Thread(target=lambda: logger.info("thread"))
        worker.start()
        worker.join()
    logger.info("after")
    lines = {line.split("| ", 1)[1].split(" [")[0]: line for line in stream.getvalue().splitlines()}
    assert lines["inner"].endswith("[run_id=r1 turn_id=t1]")
    assert lines["outer"].endswith("[run_id=r1]")
    assert "[" not in lines["thread"].split("| ", 1)[1]
    assert "[" not in lines["after"].split("| ", 1)[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("call api_key=sk-abc123 ok", "call api_key=*** ok"),
        ("token=xyz, next", "token=***, next"),
        ("Authorization: Bearer abc.def-123", "Authorization: Bearer ***"),
        ('{"secret": "hunter2"}', '{"secret": "***"}'),
        ("max_tokens=100 tokens: 5", "max_tokens=100 tokens: 5"),
    ],
)
def test_redact(raw, expected):
    assert uplog.redact(raw) == expected


def test_redaction_applied_to_formatted_args(capture):
    logger, stream, _ = capture
    logger.info("login %s", "password=p4ss")
    assert "p4ss" not in stream.getvalue() and "password=***" in stream.getvalue()


def test_console_log_command(clean_root, tmp_path):
    from uep.console.commands import Console

    lines: list[str] = []
    console = Console(None, out=lines.append)  # type: ignore[arg-type]
    uplog.configure_logging(SimpleNamespace(log_dir=tmp_path, log_file=False))
    console.execute("/log level uep.logtest TRACE")
    assert logging.getLogger("uep.logtest").level == uplog.TRACE
    console.execute("/log console DEBUG")
    console.execute("/log level x NOPE")
    assert "無法辨識" in lines[-1]
