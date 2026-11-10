"""EcoQoS 關閉：非 Windows 不做事、停用設定略過、失敗不拋例外、組裝只套用一次。"""
from __future__ import annotations

import sys

import uep.echo.inference_wiring as iw
from uep import ecoqos
from uep.echo.inference_wiring import InferenceConfig


def test_停用設定略過():
    assert ecoqos.disable_ecoqos(enabled=False) is False


def test_非_windows_不做任何事(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert ecoqos.disable_ecoqos() is False


def test_windows_api_失敗不拋例外(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")

    def boom(*_a, **_k):
        raise OSError("無 kernel32")

    monkeypatch.setattr(ecoqos.ctypes, "WinDLL", boom, raising=False)
    assert ecoqos.disable_ecoqos() is False


def test_組裝每個行程只套用一次且可關閉(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(ecoqos, "disable_ecoqos", lambda *a, **k: calls.append(1) or True)
    monkeypatch.setattr(iw, "_ECOQOS_APPLIED", False)
    iw._apply_ecoqos_once(InferenceConfig(disable_ecoqos=False))
    assert calls == []
    iw._apply_ecoqos_once(InferenceConfig())
    iw._apply_ecoqos_once(InferenceConfig())
    assert calls == [1]
