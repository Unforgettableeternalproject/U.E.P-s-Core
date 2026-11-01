#Requires -Version 7
<#
.SYNOPSIS
    子系統遷入 Core 的本地閘門：依序在各子系統目錄用共用環境跑其原本的測試命令，再跑 Core 的 tests/uep，彙整 pass／skip／fail 與耗時。

.DESCRIPTION
    對應 docs/架構重構/子系統遷入Core計畫.md §5.3（基線命令）與 §7.4（本地閘門）。
    每一項各自獨立行程（五個子系統的 tests/ 同名、conftest 各自獨立，不可同進程混跑）。
    預設設 CUDA_VISIBLE_DEVICES=""，GPU 測試自動 skip、不佔用顯卡；需要 GPU 時加 -AllowGpu。
    不修改任何被測 repo：停用 pytest cacheprovider、不寫 .pyc、Session 的 --cov 以 -o addopts= 覆蓋（共用環境沒有 pytest-cov）。

.PARAMETER Layout
    Sibling：子系統在 Core 的兄弟 repo（遷入前，預設）。Core：子系統在 <Core>/subsystems/<name>（遷入後）。
    已遷入的子系統（見 $migrated；目前為 session）不看此參數，一律走 <Core>/subsystems/<name>。
    也可用環境變數 UEP_GATE_LAYOUT 指定。

.PARAMETER Only
    只跑指定項目（es、em、stt、session、tts、core），預設全跑。

.PARAMETER SiblingRoot
    Sibling 佈局下兄弟 repo 的父目錄，預設為 Core 的上一層。

.PARAMETER TimeoutSec
    單項逾時秒數，逾時即終止該行程樹並記為 TIMEOUT。預設 600。

.PARAMETER PytestExtra
    附加給每一項 pytest 的額外參數（例如 '--durations=15'），預設不加。

.PARAMETER CoreTimeoutSec
    Core 的 tests/uep 單項逾時秒數（共用 PostgreSQL，整套約 10 分鐘，已貼近 600 秒，故獨立設定）。預設 1800。

.PARAMETER LogDir
    各項完整輸出的存放目錄，預設 $env:TEMP\uep-gate\<時間戳>。

.EXAMPLE
    pwsh -File scripts/run_all_tests.ps1
    pwsh -File scripts/run_all_tests.ps1 -Layout Core -Only es,core
#>
[CmdletBinding()]
param(
    [ValidateSet('Sibling', 'Core')]
    [string]$Layout = $(if ($env:UEP_GATE_LAYOUT) { $env:UEP_GATE_LAYOUT } else { 'Sibling' }),
    [ValidateSet('es', 'em', 'stt', 'session', 'tts', 'core')]
    [string[]]$Only,
    [string]$SiblingRoot,
    [string]$Python = (Join-Path $env:USERPROFILE '.uep\venv-inference-312\Scripts\python.exe'),
    [int]$TimeoutSec = 600,
    [int]$CoreTimeoutSec = 1800,
    [string]$PytestExtra = '',
    [string]$LogDir,
    [switch]$AllowGpu
)

$ErrorActionPreference = 'Stop'
$coreRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not $SiblingRoot) { $SiblingRoot = (Resolve-Path (Join-Path $coreRoot '..')).Path }
if (-not (Test-Path $Python)) { throw "找不到共用環境 Python：$Python" }
if (-not $LogDir) { $LogDir = Join-Path $env:TEMP ("uep-gate\" + (Get-Date -Format 'yyyyMMdd-HHmmss')) }
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# 已遷入 Core 的子系統：不論 Layout 一律走 subsystems/<name>（M1：Session）。後續里程碑遷入時在此追加
$migrated = @('session')

# 子系統名稱 -> 兄弟 repo 目錄名 / 遷入後 subsystems 下的目錄名 / 原本的 pytest 參數
# Session 的 addopts 在遷入後已移除 --cov；此處仍以 -o addopts= 清掉並顯式保留 e2e 排除，與 M0 基線命令一致
$items = @(
    [pscustomobject]@{ Key = 'es';      Label = 'ES (echo_stream)';              Sibling = 'Echo-Stream';              Sub = 'echo_stream';       PytestArgs = '' }
    [pscustomobject]@{ Key = 'em';      Label = 'EM (echo_memory 三包)';         Sibling = 'TestSeperateMemorySystem'; Sub = 'echo_memory';       PytestArgs = '' }
    [pscustomobject]@{ Key = 'stt';     Label = 'STT (echo_stt)';                Sibling = 'TestSeparateSTTSystem';    Sub = 'echo_stt';          PytestArgs = '' }
    [pscustomobject]@{ Key = 'session'; Label = 'Session (echo_thought_core)';   Sibling = 'TestSeparateSessionControl'; Sub = 'echo_thought_core'; PytestArgs = '-o addopts= -m "not e2e"' }
    [pscustomobject]@{ Key = 'tts';     Label = 'TTS (echo_tts)';                Sibling = 'TestSeparateTTSSystem';    Sub = 'echo_tts';          PytestArgs = '' }
    [pscustomobject]@{ Key = 'core';    Label = 'Core (tests/uep)';              Sibling = $null;                      Sub = $null;               PytestArgs = 'tests/uep' }
)
if ($Only) { $items = $items | Where-Object { $Only -contains $_.Key } }

function Resolve-WorkDir($item) {
    if ($item.Key -eq 'core') { return $coreRoot }
    if ($Layout -eq 'Core' -or $migrated -contains $item.Key) { return Join-Path $coreRoot (Join-Path 'subsystems' $item.Sub) }
    return Join-Path $SiblingRoot $item.Sibling
}

function Get-GitHead($dir) {
    $sha = & git -C $dir rev-parse --short HEAD 2>$null
    if ($LASTEXITCODE -ne 0) { return '?' }
    $dirty = (& git -C $dir status --porcelain 2>$null | Measure-Object).Count
    return $(if ($dirty -gt 0) { "$sha (+$dirty 未提交)" } else { $sha })
}

function Read-Summary([string]$text) {
    # 取輸出最後的 pytest 摘要列，例如「1 failed, 20 passed, 3 skipped, 2 deselected in 3.2s (0:00:03)」
    $counts = @{ passed = 0; skipped = 0; failed = 0; error = 0; deselected = 0; xfailed = 0; xpassed = 0 }
    $line = ($text -split "`r?`n" | Where-Object { $_ -match '\b(passed|failed|skipped|error|errors|no tests ran)\b.*\bin [\d.]+s' } | Select-Object -Last 1)
    if (-not $line) { return $null }
    foreach ($m in [regex]::Matches($line, '(\d+) (passed|skipped|failed|errors?|deselected|xfailed|xpassed)')) {
        $name = $m.Groups[2].Value -replace '^errors$', 'error'
        $counts[$name] = [int]$m.Groups[1].Value
    }
    return $counts
}

# 環境：CPU-only 預設、UTF-8 輸出、不產生 .pyc（不弄髒被測 repo）
if (-not $AllowGpu) { $env:CUDA_VISIBLE_DEVICES = '' }
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONDONTWRITEBYTECODE = '1'

Write-Host "== 本地閘門  Layout=$Layout  GPU=$(if ($AllowGpu) { '允許' } else { '停用 (CUDA_VISIBLE_DEVICES=空)' })  逾時=${TimeoutSec}s (Core ${CoreTimeoutSec}s)"
Write-Host "   Python : $Python"
Write-Host "   機器   : $env:COMPUTERNAME  日期 : $(Get-Date -Format 'yyyy-MM-dd HH:mm')"
Write-Host "   記錄   : $LogDir"

$results = @()
foreach ($item in $items) {
    $dir = Resolve-WorkDir $item
    if (-not (Test-Path $dir)) {
        Write-Host "[$($item.Key)] 找不到目錄：$dir" -ForegroundColor Red
        $results += [pscustomobject]@{ Key = $item.Key; Label = $item.Label; Commit = '-'; Status = 'MISSING'; Passed = 0; Skipped = 0; Failed = 0; Error = 0; Deselected = 0; Seconds = 0 }
        continue
    }
    $commit = Get-GitHead $dir
    $out = Join-Path $LogDir "$($item.Key).out.txt"
    $err = Join-Path $LogDir "$($item.Key).err.txt"
    $argText = "-m pytest -p no:cacheprovider $($item.PytestArgs) $PytestExtra".Trim()
    Write-Host "[$($item.Key)] $dir"
    Write-Host "      python $argText"

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $Python -ArgumentList $argText -WorkingDirectory $dir -NoNewWindow -PassThru `
        -RedirectStandardOutput $out -RedirectStandardError $err
    $null = $proc.Handle   # 先取得 handle，之後才讀得到 ExitCode
    $limit = if ($item.Key -eq 'core') { $CoreTimeoutSec } else { $TimeoutSec }
    $finished = $proc.WaitForExit($limit * 1000)
    if (-not $finished) {
        & taskkill /PID $proc.Id /T /F 2>&1 | Out-Null
        $proc.WaitForExit(5000) | Out-Null
    }
    $sw.Stop()

    $text = (Get-Content -Raw -Encoding utf8 $out -ErrorAction SilentlyContinue) + "`n" + (Get-Content -Raw -Encoding utf8 $err -ErrorAction SilentlyContinue)
    $c = Read-Summary $text
    if (-not $finished) { $status = 'TIMEOUT' }
    elseif ($null -eq $c) { $status = 'NOSUMMARY' }
    elseif ($proc.ExitCode -eq 0) { $status = 'OK' }
    else { $status = 'FAIL' }
    if ($null -eq $c) { $c = @{ passed = 0; skipped = 0; failed = 0; error = 0; deselected = 0 } }

    $results += [pscustomobject]@{
        Key = $item.Key; Label = $item.Label; Commit = $commit; Status = $status
        Passed = $c.passed; Skipped = $c.skipped; Failed = $c.failed; Error = $c.error; Deselected = $c.deselected
        Seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1)
    }
    $color = if ($status -eq 'OK') { 'Green' } else { 'Red' }
    Write-Host ("      -> {0}  passed={1} skipped={2} failed={3} error={4}  {5}s" -f $status, $c.passed, $c.skipped, $c.failed, $c.error, $results[-1].Seconds) -ForegroundColor $color
}

Write-Host ""
Write-Host "== 彙總 (Layout=$Layout)"
$results | Format-Table Key, Label, Status, Passed, Skipped, Failed, Error, Deselected, Seconds, Commit -AutoSize | Out-String | Write-Host

$summaryPath = Join-Path $LogDir 'summary.md'
$md = @('| 項目 | 狀態 | passed | skipped | failed | error | deselected | 耗時(s) | commit |', '|---|---|---|---|---|---|---|---|---|')
$md += $results | ForEach-Object { "| $($_.Label) | $($_.Status) | $($_.Passed) | $($_.Skipped) | $($_.Failed) | $($_.Error) | $($_.Deselected) | $($_.Seconds) | $($_.Commit) |" }
$md | Set-Content -Encoding utf8 $summaryPath
Write-Host "Markdown 彙總：$summaryPath"

$bad = $results | Where-Object { $_.Status -ne 'OK' }
if ($bad) { Write-Host "未通過：$((($bad | ForEach-Object { $_.Key }) -join ', '))" -ForegroundColor Red; exit 1 }
exit 0
