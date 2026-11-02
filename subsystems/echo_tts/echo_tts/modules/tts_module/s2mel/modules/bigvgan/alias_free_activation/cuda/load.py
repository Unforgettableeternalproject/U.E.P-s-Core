# Copyright (c) 2024 NVIDIA CORPORATION.
#   Licensed under the MIT license.

import hashlib
import os
import pathlib
import subprocess

from torch.utils import cpp_extension

"""
Setting this param to a list has a problem of generating different compilation commands (with diferent order of architectures) and leading to recompilation of fused kernels.
Set it to empty stringo avoid recompilation and assign arch flags explicity in extra_cuda_cflags below
"""
os.environ["TORCH_CUDA_ARCH_LIST"] = ""


def _stable_build_dir(srcpath: pathlib.Path) -> pathlib.Path:
    """Build directory 放在使用者快取中，不受 git 操作影響。

    路徑格式: ~/.cache/torch_extensions/bigvgan_aa_{hash8}
    hash 來自 source 目錄的絕對路徑，確保不同 repo 不衝突。
    """
    path_hash = hashlib.md5(str(srcpath).encode()).hexdigest()[:8]
    cache_root = pathlib.Path(os.environ.get(
        "TORCH_EXTENSIONS_DIR",
        pathlib.Path.home() / ".cache" / "torch_extensions"
    ))
    buildpath = cache_root / f"bigvgan_aa_{path_hash}"
    buildpath.mkdir(parents=True, exist_ok=True)
    return buildpath


# nvcc 支援的 Visual Studio 版本範圍（vswhere 的 installationVersion，半開區間）。
# CUDA 12.x 的 host_config.h 只接受 VS 2017–2022（15.x–17.x），VS 2026 = 18.x 會觸發
# `fatal error C1189: unsupported Microsoft Visual Studio version`。
# 未來 CUDA 支援新版 VS 時再調整上限。
_NVCC_VS_VERSION_RANGE = "[15.0,18.0)"
# 手動指定 VS 安裝路徑（例如 C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools）
_VS_INSTALL_PATH_ENV = "ECHO_TTS_VS_INSTALL_PATH"


def _find_nvcc_compatible_vs():
    """找出 nvcc 支援範圍內的 VS 安裝路徑；找不到回傳 None。"""
    override = os.environ.get(_VS_INSTALL_PATH_ENV, "").strip()
    if override:
        return pathlib.Path(override)

    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    if not program_files_x86:
        return None
    vswhere = (
        pathlib.Path(program_files_x86)
        / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    )
    if not vswhere.is_file():
        return None

    result = subprocess.run(
        [
            str(vswhere),
            "-version", _NVCC_VS_VERSION_RANGE,
            "-products", "*",
            "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
            "-latest",
            "-property", "installationPath",
        ],
        capture_output=True,
        text=True,
        errors="replace",
    )
    lines = [ln.strip() for ln in (result.stdout or "").splitlines() if ln.strip()]
    if result.returncode != 0 or not lines:
        return None
    return pathlib.Path(lines[0])


def _capture_vcvars_env(vcvars: pathlib.Path) -> dict:
    """執行 vcvars64.bat 並擷取 `set` 輸出成 dict。

    用 `cmd /u` 讓內建指令以 UTF-16LE 輸出，避免 cp950 等 codepage 讓中文路徑變亂碼。
    """
    result = subprocess.run(
        f'cmd.exe /u /d /s /c ""{vcvars}" >nul 2>&1 && set"',
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"vcvars64.bat 結束碼 {result.returncode}")
    output = result.stdout.decode("utf-16-le", errors="replace")
    env = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        # 略過 cmd 內部的 `=C:` 類磁碟機變數與非 key=value 行
        if not sep or not key:
            continue
        env[key] = value
    return env


def _ensure_nvcc_compatible_msvc_env():
    """Windows 上確保 JIT 編譯使用 nvcc 支援的 MSVC。

    torch.utils.cpp_extension 在環境沒有 VSCMD_ARG_TGT_ARCH 時，會自行載入「最新」的
    VS（可能是 nvcc 不支援的 VS 2026）；已有 VSCMD_ARG_TGT_ARCH 則沿用當前環境。
    因此在這裡先載入相容版本的 vcvars64 環境。任何失敗都只警告，照舊走 torch 預設流程。
    """
    if os.name != "nt" or "VSCMD_ARG_TGT_ARCH" in os.environ:
        return

    try:
        vs_path = _find_nvcc_compatible_vs()
    except Exception as exc:  # vswhere 執行異常不應阻斷原本流程
        print(f"[BigVGAN CUDA] 警告：vswhere 查詢失敗（{exc}），改用 torch 預設的 MSVC 偵測。")
        return
    if vs_path is None:
        print(
            f"[BigVGAN CUDA] 警告：找不到 nvcc 支援範圍 {_NVCC_VS_VERSION_RANGE} 內含 C++ 工具的 "
            f"Visual Studio，改用 torch 預設的 MSVC 偵測（若預設選到 VS 2026 會編譯失敗）。"
            f"可設定 {_VS_INSTALL_PATH_ENV} 手動指定 VS 安裝路徑。"
        )
        return

    vcvars = vs_path / "VC" / "Auxiliary" / "Build" / "vcvars64.bat"
    if not vcvars.is_file():
        print(f"[BigVGAN CUDA] 警告：找不到 {vcvars}，改用 torch 預設的 MSVC 偵測。")
        return

    try:
        vc_env = _capture_vcvars_env(vcvars)
    except Exception as exc:
        print(f"[BigVGAN CUDA] 警告：執行 {vcvars} 失敗（{exc}），改用 torch 預設的 MSVC 偵測。")
        return
    if "VSCMD_ARG_TGT_ARCH" not in vc_env:
        print(
            f"[BigVGAN CUDA] 警告：{vcvars} 未設定 VSCMD_ARG_TGT_ARCH，"
            f"改用 torch 預設的 MSVC 偵測。"
        )
        return

    # 整批套用差異（PATH/INCLUDE/LIB/LIBPATH/VSCMD_* 等）
    for key, value in vc_env.items():
        if os.environ.get(key) != value:
            os.environ[key] = value
    # torch 建議 VC 環境已啟用時設定，避免 setuptools 再次偵測並啟用其他 VS
    os.environ.setdefault("DISTUTILS_USE_SDK", "1")
    print(f"[BigVGAN CUDA] 已載入 nvcc 相容的 MSVC 環境：{vs_path}")


def load():
    # Check if cuda 11 is installed for compute capability 8.0
    cc_flag = []
    _, bare_metal_major, _ = _get_cuda_bare_metal_version(cpp_extension.CUDA_HOME)
    if int(bare_metal_major) >= 11:
        cc_flag.append("-gencode")
        cc_flag.append("arch=compute_80,code=sm_80")

    # Build path — 使用穩定的外部快取，不受 git checkout/merge 影響
    srcpath = pathlib.Path(__file__).parent.absolute()
    buildpath = _stable_build_dir(srcpath)

    # Windows：確保 JIT 使用 nvcc 支援的 MSVC（避免被 VS 2026 搶走）
    _ensure_nvcc_compatible_msvc_env()

    # Helper function to build the kernels.
    def _cpp_extention_load_helper(name, sources, extra_cuda_flags):
        return cpp_extension.load(
            name=name,
            sources=sources,
            build_directory=buildpath,
            extra_cflags=[
                "-O3",
            ],
            extra_cuda_cflags=[
                "-O3",
                "-gencode",
                "arch=compute_70,code=sm_70",
                "--use_fast_math",
            ]
            + extra_cuda_flags
            + cc_flag,
            verbose=True,
        )

    extra_cuda_flags = [
        "-U__CUDA_NO_HALF_OPERATORS__",
        "-U__CUDA_NO_HALF_CONVERSIONS__",
        "--expt-relaxed-constexpr",
        "--expt-extended-lambda",
    ]

    sources = [
        srcpath / "anti_alias_activation.cpp",
        srcpath / "anti_alias_activation_cuda.cu",
    ]
    anti_alias_activation_cuda = _cpp_extention_load_helper(
        "anti_alias_activation_cuda", sources, extra_cuda_flags
    )

    return anti_alias_activation_cuda


def _get_cuda_bare_metal_version(cuda_dir):
    raw_output = subprocess.check_output(
        [cuda_dir + "/bin/nvcc", "-V"], universal_newlines=True
    )
    output = raw_output.split()
    release_idx = output.index("release") + 1
    release = output[release_idx].split(".")
    bare_metal_major = release[0]
    bare_metal_minor = release[1][0]

    return raw_output, bare_metal_major, bare_metal_minor
