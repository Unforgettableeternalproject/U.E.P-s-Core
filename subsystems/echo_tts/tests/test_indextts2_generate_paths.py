from pathlib import Path

from echo_tts.runtime.indextts_generate import default_generation_paths, validate_generation_paths


def test_default_generation_paths_point_to_copied_assets():
    root = Path(__file__).resolve().parents[1]
    paths = default_generation_paths(root)

    assert paths.cfg_path.name == "config.yaml"
    # IndexTTS 2.5 遷移後的預設資產
    assert paths.model_dir.name == "checkpoints_v25"
    assert paths.character_path.name == "uep_v25.pt"
    validate_generation_paths(paths)
