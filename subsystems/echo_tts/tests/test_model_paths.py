"""權重位置解析（遷入 Core 後權重不進 git，路徑由環境變數指定）。"""
from pathlib import Path

from echo_tts.config import model_paths
from echo_tts.runtime.indextts_generate import default_generation_paths


def test_vox_預設在使用者目錄(monkeypatch):
    monkeypatch.delenv(model_paths.ENV_MODELS_DIR, raising=False)
    model_dir, lora_dir = model_paths.default_vox_paths()
    assert model_dir == Path.home() / ".uep" / "models" / "vox" / "VoxCPM2"
    assert lora_dir == Path.home() / ".uep" / "models" / "vox" / "lora_tw" / "step_0000600"


def test_vox_環境變數覆寫且明確設定優先(monkeypatch, tmp_path):
    monkeypatch.setenv(model_paths.ENV_MODELS_DIR, str(tmp_path))
    merged = model_paths.with_default_vox_paths({"lora_dir": "custom"})
    assert merged["model_dir"] == str(tmp_path / "VoxCPM2")
    assert merged["lora_dir"] == "custom"


def test_indextts_預設在使用者目錄_環境變數可改(monkeypatch, tmp_path):
    monkeypatch.delenv(model_paths.ENV_INDEXTTS_DIR, raising=False)
    default = default_generation_paths()
    weights = Path.home() / ".uep" / "models" / "indextts2"
    assert model_paths.indextts_weights_root() == weights
    assert default.model_dir == weights / "modules" / "tts_module" / "checkpoints_v25"
    assert default.character_path == weights / "models" / "tts" / "uep_v25.pt"
    assert default.engine_root.name == "echo_tts" and default.engine_root.parent == default.project_root
    monkeypatch.setenv(model_paths.ENV_INDEXTTS_DIR, str(tmp_path))
    moved = default_generation_paths()
    assert moved.model_dir == tmp_path / "modules" / "tts_module" / "checkpoints_v25"
    assert moved.character_path == tmp_path / "models" / "tts" / "uep_v25.pt"
    assert moved.engine_root == default.engine_root  # 程式碼位置不隨權重位置移動
