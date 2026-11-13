$ErrorActionPreference = "Stop"

function Resolve-LethePython {
    param([string]$PreferredVenvPython)

    if ($PreferredVenvPython -and (Test-Path $PreferredVenvPython)) {
        return $PreferredVenvPython
    }

    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCmd) {
        try {
            $exe = (& py -3 -c "import sys; print(sys.executable)" 2>$null).Trim()
            if ($exe -and (Test-Path $exe)) {
                return $exe
            }
        } catch {
        }
    }

    $pythonCmds = Get-Command python -All -ErrorAction SilentlyContinue
    foreach ($cmd in $pythonCmds) {
        if ($cmd.Source -and ($cmd.Source -notlike "*\WindowsApps\*")) {
            try {
                & $cmd.Source -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
                if ($LASTEXITCODE -eq 0) {
                    return $cmd.Source
                }
            } catch {
            }
        }
    }

    throw "Python 3.10+ was not found. Install Python from https://www.python.org/downloads/ and re-run."
}

function Find-LocalLetheRepo {
    $candidates = @()
    if ($PSScriptRoot) { $candidates += $PSScriptRoot }
    $candidates += (Get-Location).Path

    foreach ($dir in $candidates) {
        $pyproject = Join-Path $dir "pyproject.toml"
        if (Test-Path $pyproject) {
            $text = Get-Content -Path $pyproject -Raw -ErrorAction SilentlyContinue
            if ($text -match '(?m)^name\s*=\s*"lethe"') {
                return (Resolve-Path $dir).Path
            }
        }
    }
    return $null
}

Write-Host "================================================================================"
Write-Host " LETHE WINDOWS CLI INSTALLER"
Write-Host "================================================================================"

$RepoRoot = Find-LocalLetheRepo
$VenvDir = if ($RepoRoot) { Join-Path $RepoRoot ".venv" } else { Join-Path $env:USERPROFILE ".lethe\venv" }
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"

$BasePython = Resolve-LethePython -PreferredVenvPython $VenvPython

if (-not (Test-Path $VenvPython)) {
    Write-Host "Creating virtual environment at $VenvDir ..."
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $VenvDir) | Out-Null
    & $BasePython -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create virtual environment at $VenvDir."
    }
}

Write-Host "Installing Lethe package and dependencies ..."
if ($RepoRoot) {
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install -e $RepoRoot
} else {
    $gitCmd = Get-Command git -ErrorAction SilentlyContinue
    $installSpec = if ($gitCmd) {
        "git+https://github.com/Vrajesh-sulakhe/Lethe.git"
    } else {
        "https://github.com/Vrajesh-sulakhe/Lethe/archive/refs/heads/main.zip"
    }
    & $VenvPython -m pip install --upgrade pip --quiet
    & $VenvPython -m pip install --upgrade $installSpec
}

if ($LASTEXITCODE -ne 0) {
    throw "pip install failed."
}

Write-Host "Configuring global 'lethe' and 'lethe-mcp' CLI commands on Windows PATH ..."
& $VenvPython -m lethe.cli install-cli
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install global CLI wrappers."
}

$LocalBin = Join-Path $env:USERPROFILE ".local\bin"
if (($env:Path -split ";") -notcontains $LocalBin) {
    $env:Path = "$LocalBin;$env:Path"
}

Write-Host ""
Write-Host "Verifying bare 'lethe' command opens the menu:"
& (Join-Path $LocalBin "lethe.cmd")

Write-Host ""
Write-Host "================================================================================"
Write-Host " DONE! Type 'lethe' in PowerShell or CMD to open the Lethe menu."
Write-Host " Next steps:"
Write-Host "   lethe setup   # Auto-wire Cursor, Claude Code, Antigravity, and Codex"
Write-Host "   lethe creds   # Print JSON / TOML / 1-line CLI MCP credentials"
Write-Host "   lethe health  # Verify universal ~/.lethe/lethe.db WAL health"
Write-Host " (If a pre-existing terminal does not see 'lethe', open a new terminal window.)"
Write-Host "================================================================================"
