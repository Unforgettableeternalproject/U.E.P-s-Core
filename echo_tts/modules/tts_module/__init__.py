"""IndexTTS2 engine package (snapshot from upstream).

This test project imports `IndexTTSLite` directly for generation parity. The
full U.E.P `TTSModule` wrapper depends on main-core services and is intentionally
not imported here.
"""

from .lite_engine import IndexTTSLite

__all__ = ["IndexTTSLite"]
