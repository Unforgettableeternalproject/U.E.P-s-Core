"""Windows EcoQoS（ProcessPowerThrottling）控制。

EcoQoS 會讓排程器把本行程當背景工作丟到 E 核，TTS／STT 延遲明顯變差。
啟動時對本行程明確關閉 EXECUTION_SPEED 節流。非 Windows 或失敗只記 log，不拋例外。
參考 bot 的 utils/ecoqos.py（commit 928b695）。
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

logger = logging.getLogger(__name__)

_PROCESS_POWER_THROTTLING = 4  # PROCESS_INFORMATION_CLASS.ProcessPowerThrottling
_THROTTLING_CURRENT_VERSION = 1
_THROTTLING_EXECUTION_SPEED = 0x1


class _ProcessPowerThrottlingState(ctypes.Structure):
    _fields_ = [
        ("Version", wintypes.ULONG),
        ("ControlMask", wintypes.ULONG),
        ("StateMask", wintypes.ULONG),
    ]


def disable_ecoqos(enabled: bool = True) -> bool:
    """關閉本行程的 EcoQoS 執行速度節流。

    ``enabled`` 為 False 直接略過。回傳是否成功套用；略過、非 Windows、失敗皆為 False，不拋例外。
    """
    if not enabled:
        logger.info("EcoQoS 停用設定已關閉，略過")
        return False
    if sys.platform != "win32":
        return False  # 非 Windows：什麼都不做
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
        ]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        # StateMask=0 + ControlMask=EXECUTION_SPEED：明確關閉節流（而非交給系統管理）
        state = _ProcessPowerThrottlingState(_THROTTLING_CURRENT_VERSION, _THROTTLING_EXECUTION_SPEED, 0)
        ok = kernel32.SetProcessInformation(
            kernel32.GetCurrentProcess(), _PROCESS_POWER_THROTTLING, ctypes.byref(state), ctypes.sizeof(state),
        )
        if ok:
            logger.info("已關閉 EcoQoS（ProcessPowerThrottling），避免排程到 E 核")
            return True
        logger.warning("關閉 EcoQoS 失敗，GetLastError=%s", ctypes.get_last_error())
    except Exception as exc:  # noqa: BLE001 — 不可影響啟動
        logger.warning("關閉 EcoQoS 時發生例外：%s", exc)
    return False
