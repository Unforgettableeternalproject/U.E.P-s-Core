"""echo_tts 設定預設值：預設引擎為 vox（indextts2 為可選）。"""
from echo_tts.config.settings import DEFAULT_CONFIG, load_config


def test_預設_backend_為_vox():
    assert DEFAULT_CONFIG["backend"] == "vox"
    assert load_config()["backend"] == "vox"


def test_設定檔可覆寫_backend(tmp_path):
    path = tmp_path / "tts.yaml"
    path.write_text("backend: indextts2\n", encoding="utf-8")
    assert load_config(path)["backend"] == "indextts2"
