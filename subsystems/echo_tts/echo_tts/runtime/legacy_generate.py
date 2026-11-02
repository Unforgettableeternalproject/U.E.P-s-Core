# 向後相容 shim — 實際內容已遷移至 indextts_generate
# 舊名稱保留為別名，新程式碼請使用 indextts_generate 中的對應型別與函式
import warnings as _warnings

_warnings.warn(
    "echo_tts.runtime.legacy_generate 已遷移至 echo_tts.runtime.indextts_generate，"
    "請將 import 更新為 indextts_generate。"
    " legacy_generate 將在未來版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

from echo_tts.runtime.indextts_generate import (  # noqa: F401, E402
    GenerationPaths,
    default_generation_paths,
    validate_generation_paths,
    generate_with_indextts2,
)

# 向後相容別名
LegacyGenerationPaths = GenerationPaths  # noqa: N816
generate_with_legacy_indextts2 = generate_with_indextts2  # noqa: N816

__all__ = [
    "GenerationPaths",
    "LegacyGenerationPaths",
    "default_generation_paths",
    "validate_generation_paths",
    "generate_with_indextts2",
    "generate_with_legacy_indextts2",
]
