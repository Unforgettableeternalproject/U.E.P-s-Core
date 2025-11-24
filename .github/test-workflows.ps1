# GitHub Actions Local Testing Script
# Test all workflows using act

param(
    [switch]$DryRun,
    [switch]$Verbose
)

$ErrorActionPreference = "Continue"

function Test-Workflow {
    param(
        [string]$Name,
        [string]$WorkflowFile,
        [string]$Event,
        [string]$EventPath
    )
    
    Write-Host ""
    Write-Host "================================================================" -ForegroundColor DarkGray
    Write-Host "Testing: $Name" -ForegroundColor Cyan
    Write-Host "Workflow: $WorkflowFile" -ForegroundColor DarkGray
    Write-Host "================================================================" -ForegroundColor DarkGray
    
    $args = @(
        $Event,
        "-W", ".github/workflows/$WorkflowFile",
        "--eventpath", $EventPath,
        "--container-architecture", "linux/amd64"
    )
    
    if ($DryRun) {
        $args += "--dryrun"
    }
    
    if ($Verbose) {
        $args += "--verbose"
    }
    
    try {
        & act @args
        
        if ($LASTEXITCODE -eq 0) {
            Write-Host "PASS: $Name" -ForegroundColor Green
        } else {
            Write-Host "FAIL: $Name (Exit Code: $LASTEXITCODE)" -ForegroundColor Red
        }
    } catch {
        Write-Host "ERROR: $Name - $_" -ForegroundColor Red
    }
}

Write-Host ""
Write-Host "U.E.P GitHub Actions Test Suite" -ForegroundColor Cyan
Write-Host "================================" -ForegroundColor Cyan
Write-Host ""

if ($DryRun) {
    Write-Host "Mode: DRY RUN (show only, no execution)" -ForegroundColor Yellow
} else {
    Write-Host "Mode: LIVE RUN (actual execution)" -ForegroundColor Green
}

# Check if act is installed
if (-not (Get-Command act -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "ERROR: act command not found" -ForegroundColor Red
    Write-Host "Please install act: https://github.com/nektos/act" -ForegroundColor Yellow
    Write-Host "  Windows: choco install act-cli" -ForegroundColor Gray
    Write-Host "       or: scoop install act" -ForegroundColor Gray
    exit 1
}

Write-Host ""
Write-Host "act is installed" -ForegroundColor Green
act --version

# Test 1: Auto version tag
Test-Workflow `
    -Name "Auto Version Tag (develop/feature)" `
    -WorkflowFile "auto-version-tag.yml" `
    -Event "push" `
    -EventPath ".github/act-events/push-develop-config.json"

# Test 2: Hotfix tag
Test-Workflow `
    -Name "Hotfix Tag (hotfix/*)" `
    -WorkflowFile "hotfix-tag.yml" `
    -Event "push" `
    -EventPath ".github/act-events/push-hotfix.json"

# Test 3: Release stable tag
Test-Workflow `
    -Name "Release Stable Tag (release -> main)" `
    -WorkflowFile "release.yml" `
    -Event "pull_request" `
    -EventPath ".github/act-events/pr-release-merge.json"

# Test 4: Update version history
Test-Workflow `
    -Name "Update Version History" `
    -WorkflowFile "update-version-history.yml" `
    -Event "push" `
    -EventPath ".github/act-events/tag-created.json"

# Summary
Write-Host ""
Write-Host "================================================================" -ForegroundColor DarkGray
Write-Host "Test Summary" -ForegroundColor Cyan
Write-Host "================================================================" -ForegroundColor DarkGray
Write-Host ""
Write-Host "All workflows tested" -ForegroundColor Green

if ($DryRun) {
    Write-Host ""
    Write-Host "Tip: Remove -DryRun to actually execute workflows" -ForegroundColor Yellow
} else {
    Write-Host ""
    Write-Host "Warning: This was a live run and may have side effects" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Documentation: .github/WORKFLOWS_TESTING.md" -ForegroundColor DarkGray
Write-Host ""
