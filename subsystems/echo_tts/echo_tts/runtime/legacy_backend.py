# 向後相容 shim — 實際內容已遷移至 indextts_backend
# 舊名稱 LegacyIndexTTS2Backend 保留為別名，新程式碼請使用 IndexTTS2Backend
import warnings as _warnings

_warnings.warn(
    "echo_tts.runtime.legacy_backend 已遷移至 echo_tts.runtime.indextts_backend，"
    "請將 import 更新為 indextts_backend.IndexTTS2Backend。"
    " legacy_backend 將在未來版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

from echo_tts.runtime.indextts_backend import IndexTTS2Backend  # noqa: F401, E402

# 向後相容別名
LegacyIndexTTS2Backend = IndexTTS2Backend  # noqa: N816

__all__ = ["IndexTTS2Backend", "LegacyIndexTTS2Backend"]
