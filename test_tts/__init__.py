"""
test_tts — 向後相容 shim（已遷移至 echo_tts）

此套件保留作為過渡期橋接，實際內容已移至 echo_tts。
新程式碼請直接使用 echo_tts。
"""
import warnings

warnings.warn(
    "test_tts 已遷移至 echo_tts，請將 import 路徑更新為 echo_tts.*。"
    " test_tts 將在未來版本中移除。",
    DeprecationWarning,
    stacklevel=2,
)

__version__ = "0.1.0"
