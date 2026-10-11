[CmdletBinding()]
param(
    [string]$OutputPath = "release\mingjian-web-demo-20260801-r2.zip"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$output = [IO.Path]::GetFullPath((Join-Path $root $OutputPath))

if (Test-Path -LiteralPath $output) {
    throw "Refusing to overwrite an existing release: $output"
}

$requiredFiles = @(
    'RUN_ME_FIRST.txt',
    'README.md',
    '.env.example',
    'requirements-demo-lock.txt',
    'setup.ps1',
    'start-demo.ps1',
    'start-online.ps1',
    'stop-all.ps1'
)
$requiredDirectories = @(
    'backend',
    'frontend',
    'workers',
    'scripts',
    'models',
    'wheels',
    'tools',
    'runtime',
    'manifests'
)

foreach ($relative in $requiredFiles + $requiredDirectories) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $relative))) {
        throw "Required release input is missing: $relative"
    }
}

$parent = Split-Path -Parent $output
if (-not (Test-Path -LiteralPath $parent)) {
    [void](New-Item -ItemType Directory -Path $parent)
}

$tarArguments = @(
    '-a', '-c', '-f', $output,
    '--exclude=backend/data',
    '--exclude=__pycache__',
    '--exclude=*.pyc'
) + $requiredFiles + $requiredDirectories

Push-Location $root
try {
    & tar.exe @tarArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Release archive creation failed with exit code $LASTEXITCODE."
    }
} finally {
    Pop-Location
}

Write-Host "Release created: $output"
