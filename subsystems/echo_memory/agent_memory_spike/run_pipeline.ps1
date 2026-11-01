# 排程入口：每日凌晨跑記憶管線。
#
# Phase 3 定案：走 Windows 工作排程器，不掛 SessionEnd hook——
# 蒸餾要幾分鐘，hook 得 detach、失敗靜默難追，而且剛結束工作時機器最忙。
#
# 註冊（手動執行一次即可）：
#   schtasks /create /tn "AgentMemoryPipeline" /sc daily /st 03:30 ^
#     /tr "powershell -NoProfile -ExecutionPolicy Bypass -File <本檔絕對路徑>"
#
# 兩個實測逼出來的細節：
# 1. 本檔必須存成 UTF-8 **含 BOM**——Windows PowerShell 5.1 讀無 BOM 的
#    UTF-8 會當成 ANSI，中文註解的位元組會吃掉後面的程式碼行
#    （實測 $log 整行被吞成 Null，錯誤只在排程的黑箱裡發生）
# 2. Python 的輸出用 cmd /c 原生重導，不用 PowerShell 的 *>>——
#    後者把 stderr 每一行包成 NativeCommandError 紀錄，log 全是包裝噪音

$ErrorActionPreference = "Continue"
$env:PYTHONIOENCODING = "utf-8"

$repo = Split-Path -Parent $PSScriptRoot
$python = Join-Path (Split-Path -Parent $repo) "U.E.P-s-Core\env\Scripts\python.exe"
$script = Join-Path $PSScriptRoot "pipeline.py"
$logDir = Join-Path $env:USERPROFILE ".claude\agent-memory-spike\logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null

# 一天一檔。管線本身有 lockfile，重複觸發不會疊跑，log 用 append 也不會互相蓋掉
$log = Join-Path $logDir ("pipeline-{0}.log" -f (Get-Date -Format "yyyyMMdd"))

# --max-groups 24 對齊手動時代的單批大小：實測 20 組單次裁決要 ~13 分鐘，
# 40 組會頂到逾時；24 是歷史上驗證過的評審批次
"=== pipeline start $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
& cmd /c "`"$python`" `"$script`" --run --max-groups 24 >> `"$log`" 2>&1"
$code = $LASTEXITCODE
"=== pipeline exit $code $(Get-Date -Format o) ===" | Out-File -FilePath $log -Append -Encoding utf8
exit $code
