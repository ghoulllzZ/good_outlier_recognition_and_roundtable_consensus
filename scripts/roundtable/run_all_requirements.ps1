$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path (Split-Path $scriptDir -Parent) -Parent
$reqDir = Join-Path $workspaceRoot "data\raw\roundtable_conference\requirements"
$treatment = if ($env:ROUNDTABLE_TREATMENT) { $env:ROUNDTABLE_TREATMENT } else { "full_method" }
$treatmentSafe = ($treatment -replace '[^A-Za-z0-9._-]', '_')
$outRoot = Join-Path (Join-Path $workspaceRoot "outputs\reports\roundtable_conference") $treatmentSafe
$round0CacheRoot = if ($env:ROUNDTABLE_ROUND0_CACHE_ROOT) { $env:ROUNDTABLE_ROUND0_CACHE_ROOT } else { Join-Path $workspaceRoot "outputs\caches\round0" }
$requireRound0Cache = $false
if ($env:ROUNDTABLE_REQUIRE_ROUND0_CACHE) {
    $requireRound0Cache = @("1", "true", "yes", "on") -contains $env:ROUNDTABLE_REQUIRE_ROUND0_CACHE.ToLowerInvariant()
}
$singleRater = if ($env:ROUNDTABLE_SINGLE_RATER) { $env:ROUNDTABLE_SINGLE_RATER } else { "" }
$models = Join-Path $scriptDir "models.json"
$script = Join-Path $scriptDir "roundtable_req_reconcile.py"
$casePauseSeconds = 20
if ($env:ROUNDTABLE_CASE_PAUSE_SECONDS) {
    $casePauseSeconds = [int]$env:ROUNDTABLE_CASE_PAUSE_SECONDS
}

if (-not (Test-Path $reqDir)) {
    throw "Requirements directory not found: $reqDir"
}

if (-not (Test-Path $models)) {
    throw "models.json not found: $models"
}

if (-not (Test-Path $script)) {
    throw "roundtable_req_reconcile.py not found: $script"
}

New-Item -ItemType Directory -Force -Path $outRoot, $round0CacheRoot | Out-Null

$csvFiles = Get-ChildItem -Path $reqDir -Filter *.csv -File | Sort-Object Name
if (-not $csvFiles) {
    throw "No CSV files found in $reqDir"
}

foreach ($file in $csvFiles) {
    $csv = $file.FullName
    $name = [System.IO.Path]::GetFileNameWithoutExtension($file.Name)
    $safe = $name.Trim().TrimEnd(".")
    if ([string]::IsNullOrWhiteSpace($safe)) {
        $safe = "requirements_file"
    }

    $caseOut = Join-Path $outRoot $safe
    $caseLogs = Join-Path $caseOut "logs"
    $excelOut = Join-Path $caseOut ($safe + "_roundtable_report.xlsx")
    $caseRound0Cache = Join-Path $round0CacheRoot $safe

    New-Item -ItemType Directory -Force -Path $caseOut, $caseLogs, $caseRound0Cache | Out-Null

    Write-Host "Running: $($file.Name) [$treatment]" -ForegroundColor Cyan

    $cmd = @(
        $script,
        "--requirements", $csv,
        "--models", $models,
        "--out", $excelOut,
        "--out_dir", $caseLogs,
        "--treatment", $treatment,
        "--round0_cache_dir", $caseRound0Cache,
        "--topk", "10",
        "--rounds", "2",
        "--theta_ratio", "0.6",
        "--eps_score", "0.25",
        "--tau_jacc", "0.9"
    )

    if ($requireRound0Cache) {
        $cmd += "--require_round0_cache"
    }
    if (-not [string]::IsNullOrWhiteSpace($singleRater)) {
        $cmd += @("--single_rater", $singleRater)
    }

    & python @cmd

    if ($casePauseSeconds -gt 0) {
        Write-Host "Cooling down for $casePauseSeconds seconds..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $casePauseSeconds
    }
}

Write-Host "All requirement CSV files processed." -ForegroundColor Green
