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
    engine.store_episode.assert_called_once_with(
        user_input="[force_remember] Remember this preference",
        agent_response="Stored because: User requested it",
        session_id="session-test",
        salience=0.85,
        content_summary="Remember this preference",
    )
