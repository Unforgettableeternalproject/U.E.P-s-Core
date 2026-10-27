from unittest.mock import Mock

from llm_integration.tools.executor import ToolExecutor


def test_force_remember_uses_clean_content_as_summary():
    engine = Mock()
    engine.store_episode.return_value = "ep_test"
    executor = ToolExecutor(engine=engine, session_id="session-test")

    result = executor.handle_force_remember(
        {"content": "Remember this preference", "reason": "User requested it"}
    )

    assert result["success"] is True
    engine.store_episode.assert_called_once()
    kwargs = engine.store_episode.call_args.kwargs
    assert kwargs["user_input"] == ""
    assert kwargs["agent_response"] == ""
    assert kwargs["session_id"] == "session-test"
    assert kwargs["salience"] == 0.85
    assert kwargs["content_summary"] == "Remember this preference"
    assert kwargs["episode_kind"] == "tool_note"
    (unit,) = kwargs["evidence"]
    assert unit.source_type == "tool"
    assert unit.content == "Remember this preference"
    assert unit.attrs["reason"] == "User requested it"
    assert unit.content_origin is None  # 未說明；引擎落地時記為 unknown
