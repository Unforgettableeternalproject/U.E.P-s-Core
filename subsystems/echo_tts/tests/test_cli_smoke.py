from Entry import smoke


def test_smoke_command_runs_without_optional_console_dependency():
    assert smoke() == 0
