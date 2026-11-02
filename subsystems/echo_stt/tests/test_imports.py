"""
T-11: Import 測試

確認 echo_stt 所有子模組可正常 import 且不帶入任何 U.E.P Core 依賴。
"""

import sys
import importlib
import pytest

# 不應出現在 sys.modules 中的 Core 模組前綴
FORBIDDEN_PREFIXES = (
    "core.",
    "core.bases",
    "core.schemas",
    "core.working_context",
    "core.event_bus",
    "core.framework",
    "core.sessions",
    "core.system_loop",
    "core.status_manager",
    "configs.config_loader",
    "configs.user_settings_manager",
    "utils.debug_helper",
)


def _check_no_core_imports(module_name: str):
    """匯入模組後檢查 sys.modules 中沒有 Core 依賴"""
    # 記錄匯入前的模組集合
    before = set(sys.modules.keys())

    # 嘗試匯入
    mod = importlib.import_module(module_name)
    assert mod is not None, f"{module_name} 匯入失敗"

    # 檢查新增的模組中是否有禁止的前綴
    after = set(sys.modules.keys())
    new_modules = after - before

    violations = [
        m for m in new_modules
        if any(m == prefix or m.startswith(prefix + ".") for prefix in FORBIDDEN_PREFIXES)
    ]

    assert not violations, (
        f"匯入 {module_name} 後發現 Core 依賴洩漏: {violations}"
    )


# ── 不需要 torch/pyaudio 的輕量模組 ──

class TestLightweightImports:
    """不依賴重型套件的模組 import 測試"""

    def test_config(self):
        _check_no_core_imports("echo_stt.core.config")

    def test_schemas(self):
        _check_no_core_imports("echo_stt.core.schemas")

    def test_keyword_detector(self):
        _check_no_core_imports("echo_stt.keyword.detector")

    def test_context_handler(self):
        _check_no_core_imports("echo_stt.speaker.context_handler")


# ── 需要 numpy 但不需要 torch 的模組 ──

class TestNumpyDependentImports:
    """需要 numpy 的模組（VAD、Speaker）"""

    def test_energy_vad(self):
        _check_no_core_imports("echo_stt.vad.energy_vad")

    def test_speaker_identification(self):
        _check_no_core_imports("echo_stt.speaker.identification")


# ── 頂層匯入 ──

class TestTopLevelImport:
    """echo_stt 頂層 package 匯入

    注意: echo_stt.__init__ 匯入 STTEngine，
    STTEngine 依賴 torch/pyaudio，因此只在有這些套件時才測試。
    """

    @pytest.mark.skipif(
        not importlib.util.find_spec("torch"),
        reason="torch 不可用，跳過頂層匯入測試",
    )
    @pytest.mark.skipif(
        not importlib.util.find_spec("pyaudio"),
        reason="pyaudio 不可用，跳過頂層匯入測試",
    )
    def test_echo_stt_top_level(self):
        _check_no_core_imports("echo_stt")

    @pytest.mark.skipif(
        not importlib.util.find_spec("torch"),
        reason="torch 不可用",
    )
    @pytest.mark.skipif(
        not importlib.util.find_spec("pyaudio"),
        reason="pyaudio 不可用",
    )
    def test_engine_import(self):
        _check_no_core_imports("echo_stt.core.engine")
