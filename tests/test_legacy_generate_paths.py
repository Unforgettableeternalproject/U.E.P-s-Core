from pathlib import Path

from test_tts.runtime.legacy_generate import default_generation_paths, validate_generation_paths


def test_default_generation_paths_point_to_copied_assets():
    root = Path(__file__).resolve().parents[1]
    paths = default_generation_paths(root)

    assert paths.cfg_path.name == "config.yaml"
    assert paths.character_path.name == "uep.pt"
    validate_generation_paths(paths)
