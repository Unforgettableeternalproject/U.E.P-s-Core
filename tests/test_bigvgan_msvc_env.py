"""BigVGAN CUDA kernel 載入器的 MSVC 環境選擇測試（不實際編譯）。"""

import os
import subprocess
from unittest import mock

import pytest

from echo_tts.modules.tts_module.s2mel.modules.bigvgan.alias_free_activation.cuda import (
    load as bigvgan_load,
)


def _completed(returncode=0, stdout=b""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout)


@pytest.fixture
def fake_vs(tmp_path):
    """建立假的 VS 安裝目錄與 vswhere.exe。"""
    pf86 = tmp_path / "pf86"
    vswhere = pf86 / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    vswhere.parent.mkdir(parents=True)
    vswhere.write_bytes(b"")
    vs_root = tmp_path / "VS2022"
    vcvars = vs_root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    vcvars.parent.mkdir(parents=True)
    vcvars.write_text("@echo off\n")
    return pf86, vs_root


@pytest.fixture
def clean_env(monkeypatch):
    """模擬一般 shell：沒有 vcvars 環境、沒有手動覆寫；結束後完整還原 os.environ。"""
    with mock.patch.dict(os.environ):
        for key in ("VSCMD_ARG_TGT_ARCH", "DISTUTILS_USE_SDK", bigvgan_load._VS_INSTALL_PATH_ENV):
            os.environ.pop(key, None)
        monkeypatch.setattr(bigvgan_load.os, "name", "nt")
        yield


def _fake_run_factory(vs_root, vcvars_env_text, calls):
    def fake_run(cmd, *args, **kwargs):
        calls.append(cmd)
        if isinstance(cmd, list):  # vswhere
            assert "-version" in cmd
            assert cmd[cmd.index("-version") + 1] == bigvgan_load._NVCC_VS_VERSION_RANGE
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=f"{vs_root}\n"
            )
        # vcvars：cmd /u 輸出 UTF-16LE
        return _completed(stdout=vcvars_env_text.encode("utf-16-le"))

    return fake_run


def test_noop_when_vc_env_already_active(monkeypatch, clean_env):
    monkeypatch.setenv("VSCMD_ARG_TGT_ARCH", "x64")
    with mock.patch.object(bigvgan_load.subprocess, "run") as run:
        bigvgan_load._ensure_nvcc_compatible_msvc_env()
    run.assert_not_called()


def test_noop_on_non_windows(monkeypatch, clean_env):
    monkeypatch.setattr(bigvgan_load.os, "name", "posix")
    with mock.patch.object(bigvgan_load.subprocess, "run") as run:
        bigvgan_load._ensure_nvcc_compatible_msvc_env()
    run.assert_not_called()


def test_applies_vcvars_env_when_vswhere_finds_vs(monkeypatch, clean_env, fake_vs):
    pf86, vs_root = fake_vs
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))
    monkeypatch.setenv("INCLUDE", "old-include")
    env_text = (
        "=C:=C:\\somewhere\r\n"
        "INCLUDE=C:\\VS2022\\include\r\n"
        "LIB=C:\\VS2022\\lib\r\n"
        "VSCMD_ARG_TGT_ARCH=x64\r\n"
        "VCToolsVersion=14.44.35207\r\n"
        "ECHO_TEST_中文=路徑\r\n"
    )
    calls = []
    monkeypatch.setattr(
        bigvgan_load.subprocess, "run", _fake_run_factory(vs_root, env_text, calls)
    )

    bigvgan_load._ensure_nvcc_compatible_msvc_env()

    assert os.environ["VSCMD_ARG_TGT_ARCH"] == "x64"
    assert os.environ["INCLUDE"] == "C:\\VS2022\\include"
    assert os.environ["LIB"] == "C:\\VS2022\\lib"
    assert os.environ["VCToolsVersion"] == "14.44.35207"
    assert os.environ["ECHO_TEST_中文"] == "路徑"
    assert os.environ["DISTUTILS_USE_SDK"] == "1"
    assert "" not in os.environ
    # vcvars 指令指向相容 VS 的 vcvars64.bat
    assert str(vs_root / "VC" / "Auxiliary" / "Build" / "vcvars64.bat") in calls[1]


def test_override_env_skips_vswhere(monkeypatch, clean_env, fake_vs):
    _, vs_root = fake_vs
    monkeypatch.setenv(bigvgan_load._VS_INSTALL_PATH_ENV, str(vs_root))
    calls = []
    monkeypatch.setattr(
        bigvgan_load.subprocess,
        "run",
        _fake_run_factory(vs_root, "VSCMD_ARG_TGT_ARCH=x64\r\n", calls),
    )

    bigvgan_load._ensure_nvcc_compatible_msvc_env()

    assert len(calls) == 1 and isinstance(calls[0], str)  # 只跑 vcvars，沒跑 vswhere
    assert os.environ["VSCMD_ARG_TGT_ARCH"] == "x64"


def test_no_compatible_vs_warns_without_raising(monkeypatch, clean_env, fake_vs, capsys):
    pf86, _ = fake_vs
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))
    monkeypatch.setattr(
        bigvgan_load.subprocess,
        "run",
        lambda cmd, *a, **k: subprocess.CompletedProcess(args=cmd, returncode=0, stdout=""),
    )

    bigvgan_load._ensure_nvcc_compatible_msvc_env()

    assert "VSCMD_ARG_TGT_ARCH" not in os.environ
    assert "警告" in capsys.readouterr().out


def test_missing_vswhere_warns_without_raising(monkeypatch, clean_env, tmp_path, capsys):
    monkeypatch.setenv("ProgramFiles(x86)", str(tmp_path / "nothing"))
    with mock.patch.object(bigvgan_load.subprocess, "run") as run:
        bigvgan_load._ensure_nvcc_compatible_msvc_env()
    run.assert_not_called()
    assert "VSCMD_ARG_TGT_ARCH" not in os.environ
    assert "警告" in capsys.readouterr().out


def test_vcvars_failure_warns_without_raising(monkeypatch, clean_env, fake_vs, capsys):
    pf86, vs_root = fake_vs
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))

    def fake_run(cmd, *args, **kwargs):
        if isinstance(cmd, list):
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=f"{vs_root}\n")
        return _completed(returncode=1)

    monkeypatch.setattr(bigvgan_load.subprocess, "run", fake_run)

    bigvgan_load._ensure_nvcc_compatible_msvc_env()

    assert "VSCMD_ARG_TGT_ARCH" not in os.environ
    assert "警告" in capsys.readouterr().out


def test_vswhere_exception_warns_without_raising(monkeypatch, clean_env, fake_vs, capsys):
    pf86, _ = fake_vs
    monkeypatch.setenv("ProgramFiles(x86)", str(pf86))

    def boom(*args, **kwargs):
        raise OSError("無法執行")

    monkeypatch.setattr(bigvgan_load.subprocess, "run", boom)

    bigvgan_load._ensure_nvcc_compatible_msvc_env()

    assert "VSCMD_ARG_TGT_ARCH" not in os.environ
    assert "警告" in capsys.readouterr().out
