# 建置推理環境 .venv-infer（Python 3.12，P3 S6；不動 .venv）。
# 用法：scripts/setup_infer_env.ps1 -Es <Echo-Stream 路徑> -Memory <記憶子系統路徑> [-EsRef 9912dce] [-MemoryRef cb36629]
# ES 與記憶子系統以 git archive 取指定 commit 的快照後非 editable 安裝（不在原 repo 內建置，避免寫入對方目錄）；
# TTS／STT／Session 子系統不安裝，由 ES adapter 依 UEP_INFER_*_REPO 加入 sys.path。
param(
    [Parameter(Mandatory)][string]$Es,
    [Parameter(Mandatory)][string]$Memory,
    [string]$EsRef = "9912dce",
    [string]$MemoryRef = "cb36629"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$env:UV_PROJECT_ENVIRONMENT = ".venv-infer"
uv sync --frozen --extra inference
$py = Join-Path $root ".venv-infer/Scripts/python.exe"
$tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("uep-infer-" + [guid]::NewGuid())
New-Item -ItemType Directory $tmp | Out-Null
try {
    foreach ($item in @(@("es", $Es, $EsRef), @("mem", $Memory, $MemoryRef))) {
        $dest = Join-Path $tmp $item[0]
        New-Item -ItemType Directory $dest | Out-Null
        git -C $item[1] archive $item[2] | tar -x -C $dest
        uv pip install --python $py --no-deps $dest
    }
} finally {
    Remove-Item -Recurse -Force $tmp
}
# 記憶端的純 Python 依賴（子系統 pyproject 的相依，--no-deps 未帶入的部分）與日文 G2P
uv pip install --python $py networkx python-dotenv apscheduler rich tiktoken fugashi unidic-lite
uv pip check --python $py
