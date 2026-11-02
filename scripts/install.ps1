<#
.SYNOPSIS
  One-command install on Windows: python env, the chh command, the built UI. Run from the repository root:
      powershell -ExecutionPolicy Bypass -File scripts\install.ps1
  -DryRun prints each step and changes nothing. Mac and Linux are untested: docs/user_guide.md.
#>
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$data = if ($env:CHH_DATA_DIR) { $env:CHH_DATA_DIR } else { Join-Path $HOME 'custom-harness-data' }
$venv = Join-Path $data 'venv'
$py = Join-Path $venv 'Scripts\python.exe'

function Step($what, [scriptblock]$do) {
    Write-Host "== $what"
    if (-not $DryRun) {
        & $do
        if ($LASTEXITCODE) { throw "That step failed: $what" }
    }
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { throw 'Python is not installed. Install Python 3.12 or newer from python.org, then run this again.' }
$version = & $python.Source -c 'import sys; print(sys.version_info >= (3, 12))'
if ($version -ne 'True') { throw 'This needs Python 3.12 or newer. Install it from python.org, then run this again.' }

if (-not (Test-Path $py)) { Step "Create the Python environment in $venv" { & $python.Source -m venv $venv } }
Step 'Install chh into the environment' { & $py -m pip install --quiet -e $root }

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Write-Host 'Node.js is not installed, so the web UI was not built. The chh command works without it.'
    Write-Host 'To add the UI, install Node (LTS) and run this again:  winget install --id OpenJS.NodeJS.LTS --exact'
} else {
    Push-Location (Join-Path $root 'ui')
    try {
        Step 'Install the UI packages, exactly as locked, with install scripts off' { & $npm.Source ci --ignore-scripts }
        Step 'Build the UI' { & $npm.Source run build }
    } finally { Pop-Location }
}
Write-Host ''
Write-Host "Done. Next:  & '$py' -m custom_harness.cli exp setup --name YOUR-NAME"
Write-Host "Then look around, with no agent and no spending:  & '$py' -m custom_harness.cli exp ui --scripted"
