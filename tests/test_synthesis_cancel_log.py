"""合成被呼叫端取消時的 log 分級測試（不載入模型）。"""

from echo_tts.modules.tts_module.tts_utils.segment_guard import log_synthesis_failure


class _Aborted(Exception):
    """模擬 Echo-Stream 的取消例外。"""

    is_cancellation = True


def _run(exc):
    calls = {"debug": [], "error": []}
    result = log_synthesis_failure(
        exc,
        lambda level, msg: calls["debug"].append(msg),
        lambda msg: calls["error"].append(msg),
    )
    return result, calls


def test_cancellation_uses_debug_not_error():
    result, calls = _run(_Aborted())
    assert result is True
    assert calls["error"] == []
    assert len(calls["debug"]) == 1


def test_regular_exception_still_error():
    result, calls = _run(RuntimeError("boom"))
    assert result is False
    assert calls["debug"] == []
    assert "boom" in calls["error"][0]


def test_false_flag_is_error():
    exc = RuntimeError("x")
    exc.is_cancellation = False
    result, calls = _run(exc)
    assert result is False
    assert len(calls["error"]) == 1
