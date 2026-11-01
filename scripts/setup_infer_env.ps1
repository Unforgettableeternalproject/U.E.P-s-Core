# 建置／更新共用推理環境（Python 3.12；預設 $env:USERPROFILE\.uep\venv-inference-312，不動 .venv、env/）。
# 用法：scripts/setup_infer_env.ps1 [-Es <路徑>] [-Memory <路徑>] [-Tts <路徑>] [-Stt <路徑>] [-EnvPath <路徑>]
# 第三方套件由 Core 的 uv.lock 管理（inference extra 加預設 dev group），以 --inexact 同步，不會移除已 editable 安裝的子系統。
# Session（echo_thought_core）已遷入 Core（subsystems/echo_thought_core），由 Core 本身的 hatch editable（dev-mode-exact）提供，
# 不再有 -Session 參數；其餘子系統（ES、記憶、STT、TTS）尚未遷入，仍以 `uv pip install --no-deps -e` editable 安裝，
# 指向各 repo 主工作樹（預設為 Core 的兄弟目錄）；注意 setuptools editable 會在各 repo 內寫入／覆寫 *.egg-info。
param(
    [string]$Es = "",
    [string]$Memory = "",
    [string]$Tts = "",
    [string]$Stt = "",
    [string]$EnvPath = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$sibling = Split-Path -Parent $root
if (-not $Es) { $Es = Join-Path $sibling "Echo-Stream" }
if (-not $Memory) { $Memory = Join-Path $sibling "TestSeperateMemorySystem" }
if (-not $Tts) { $Tts = Join-Path $sibling "TestSeparateTTSSystem" }
if (-not $Stt) { $Stt = Join-Path $sibling "TestSeparateSTTSystem" }
if (-not $EnvPath) { $EnvPath = Join-Path $env:USERPROFILE ".uep\venv-inference-312" }
Set-Location $root
$env:UV_PROJECT_ENVIRONMENT = $EnvPath
uv sync --frozen --extra inference --inexact
if ($LASTEXITCODE -ne 0) { throw "uv sync 失敗" }
$py = Join-Path $EnvPath "Scripts/python.exe"
# Core 的 hatch dev-mode-exact 靠 editables 載入；缺它時 .pth 失敗、Core 與已遷入的子系統都無法 import
& $py -c "import editables"
if ($LASTEXITCODE -ne 0) { throw "缺少 editables：Core 的 editable 安裝無法載入" }
foreach ($item in @(@("ES", $Es), @("記憶", $Memory), @("STT", $Stt), @("TTS", $Tts))) {
    $name = $item[0]; $path = $item[1]
    if (-not (Test-Path (Join-Path $path "pyproject.toml"))) { throw "找不到 $name 子系統：$path" }
    uv pip install --python $py --no-deps -e $path
    if ($LASTEXITCODE -ne 0) { throw "$name editable 安裝失敗" }
}
# voxcpm 宣告的訓練／WebUI 依賴由 pyproject override 刻意排除，check 時略過這些；其餘不相容仍視為失敗
$excluded = "torchcodec|gradio|modelscope|datasets|funasr|spaces|argbind"
$issues = uv pip check --python $py 2>&1 | Where-Object { "$_" -match "requires" -and "$_" -notmatch "``voxcpm`` requires ``($excluded)" }
if ($issues) { $issues; throw "uv pip check 失敗" }
