from Entry import build_legacy_session
from echo_tts.runtime.session import TTSInteractiveSession


def test_build_legacy_session_does_not_load_model(tmp_path):
    session = build_legacy_session(project_root=tmp_path, output_dir=tmp_path / "out")

    assert isinstance(session, TTSInteractiveSession)
    assert session.status()["loaded"] is False
