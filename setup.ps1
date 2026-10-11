[CmdletBinding()]
param([switch]$RecreateVenv)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'scripts\release-common.ps1')

try {
    $required = @(
        'requirements-demo-lock.txt', 'wheels\SHA256SUMS.txt',
        'manifests\MODEL_SHA256SUMS.txt', 'manifests\TOOLS_SHA256SUMS.txt',
        'manifests\RUNTIME_SHA256SUMS.txt', 'models\cache',
        'tools\ffmpeg\ffmpeg.exe', 'tools\ffmpeg\ffprobe.exe',
        'tools\tesseract\tesseract.exe', 'tools\tesseract\tessdata\eng.traineddata',
        'tools\tesseract\tessdata\chi_sim.traineddata', 'tools\tesseract\tessdata\osd.traineddata',
        'runtime\python\python-3.11.9-amd64.exe',
        'wheels\torch-2.5.1+cpu-cp311-cp311-win_amd64.whl'
    )
    foreach ($relative in $required) { Assert-RequiredPath -Path (Join-Path $root $relative) }

    [void](Test-ReleaseHashManifest -ProjectRoot $root -ManifestPath (Join-Path $root 'wheels\SHA256SUMS.txt') -RelativeBase 'wheels')
    [void](Test-ReleaseHashManifest -ProjectRoot $root -ManifestPath (Join-Path $root 'manifests\MODEL_SHA256SUMS.txt'))
    [void](Test-ReleaseHashManifest -ProjectRoot $root -ManifestPath (Join-Path $root 'manifests\TOOLS_SHA256SUMS.txt'))
    [void](Test-ReleaseHashManifest -ProjectRoot $root -ManifestPath (Join-Path $root 'manifests\RUNTIME_SHA256SUMS.txt'))

    $installer = Join-Path $root 'runtime\python\python-3.11.9-amd64.exe'
    $installerHash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($installerHash -ne '5ee42c4eee1e6b4464bb23722f90b45303f79442df63083f05322f1785f5fdde') {
        throw 'Python installer SHA-256 mismatch.'
    }
    if (Get-Command Get-AuthenticodeSignature -ErrorAction SilentlyContinue) {
        $signature = Get-AuthenticodeSignature -LiteralPath $installer
        if ($signature.Status -eq 'HashMismatch' -or $signature.Status -eq 'NotSigned') {
            throw "Python installer signature is invalid: $($signature.Status)"
        }
        if ($signature.Status -eq 'Valid' -and $signature.SignerCertificate.Subject -notmatch 'Python Software Foundation') {
            throw 'Python installer signature has an unexpected publisher.'
        }
    }

    $discovery = Get-ReleasePythonDiscovery -ProjectRoot $root
    $candidate = $discovery.CompatibleCandidate
    if ($null -eq $candidate) {
        if (-not (Test-ReleaseInstallerBootstrapAllowed -Discovery $discovery)) {
            foreach ($result in @($discovery.Results | Where-Object { $_.Status -eq 'ProbeError' })) {
                Write-Warning "Python probe error: $($result.Label) [$($result.ErrorType)]"
            }
            throw 'Python compatibility probe failed; bootstrap was not attempted.'
        }
        $target = Join-Path $env:LOCALAPPDATA 'MingjianWeb\Python3119'
        # These are documented Python Windows installer unattended options.
        $arguments = @(
            '/quiet', 'InstallAllUsers=0', "TargetDir=`"$target`"", 'PrependPath=0',
            'Include_launcher=0', 'InstallLauncherAllUsers=0', 'AssociateFiles=0',
            'Shortcuts=0', 'Include_doc=0', 'Include_test=0', 'Include_pip=1',
            'Include_exe=1', 'Include_lib=1', 'Include_tcltk=0'
        )
        $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru
        if ($process.ExitCode -ne 0) { throw "Python installer failed with ExitCode=$($process.ExitCode)" }
        $bootstrapPython = Join-Path $target 'python.exe'
        $info = Get-ReleasePythonInfo -Executable $bootstrapPython
        if (-not (Test-ReleasePythonInfo -Info $info)) { throw 'Bootstrapped Python is not CPython 3.11.9 x64.' }
        $candidate = [pscustomobject]@{ Executable = $bootstrapPython; LauncherArgs = @(); Label = $bootstrapPython; Info = $info }
    }
    Write-Host "Using Python: $($candidate.Label)"

    $venv = Join-Path $root '.venv'
    $venvPython = Join-Path $venv 'Scripts\python.exe'
    if (Test-Path -LiteralPath $venv) {
        $venvInfo = if (Test-Path -LiteralPath $venvPython -PathType Leaf) { Get-ReleasePythonInfo -Executable $venvPython } else { $null }
        if (-not (Test-ReleasePythonInfo -Info $venvInfo)) {
            if (-not $RecreateVenv) {
                throw 'The existing .venv is unusable. Run .\setup.ps1 -RecreateVenv to rebuild it explicitly.'
            }
            if ((Split-Path -Leaf $venv) -ne '.venv' -or (Split-Path -Parent $venv) -ne $root) {
                throw 'Refusing to remove an unexpected virtual environment path.'
            }
            Remove-Item -LiteralPath $venv -Recurse -Force
        }
    }
    if (-not (Test-Path -LiteralPath $venv)) {
        & $candidate.Executable @($candidate.LauncherArgs) -m venv $venv
        if ($LASTEXITCODE -ne 0) { throw 'Creating .venv failed.' }
    }
    $venvPython = Assert-ReleaseVenvPython -ProjectRoot $root
    $wheelDir = Join-Path $root 'wheels'
    $torchWheel = Join-Path $wheelDir 'torch-2.5.1+cpu-cp311-cp311-win_amd64.whl'
    & $venvPython -m pip install --no-index --find-links $wheelDir $torchWheel
    if ($LASTEXITCODE -ne 0) { throw 'Offline Torch installation failed.' }
    & $venvPython -m pip install --no-index --find-links $wheelDir -r (Join-Path $root 'requirements-demo-lock.txt')
    if ($LASTEXITCODE -ne 0) { throw 'Offline runtime dependency installation failed.' }
    & $venvPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'pip check failed.' }

    $runtimeSmoke = @'
import torch
import transformers
import numpy
import cv2
import PIL
import fastapi
import uvicorn
import httpx
import tenacity
import pydantic
import starlette

assert torch.__version__ == '2.5.1+cpu'
assert torch.version.cuda is None
assert torch.cuda.is_available() is False
print('RUNTIME_SMOKE=OK')
print('TORCH_VERSION=' + torch.__version__)
'@
    $runtimeSmokeOutput = @($runtimeSmoke | & $venvPython - 2>&1)
    $runtimeSmokeExitCode = $LASTEXITCODE
    $runtimeSmokeLines = @($runtimeSmokeOutput | ForEach-Object { [string]$_ })
    if ($runtimeSmokeExitCode -ne 0 -or $runtimeSmokeLines -notcontains 'RUNTIME_SMOKE=OK') {
        throw 'Runtime import or Torch CPU verification failed.'
    }
    $torchLine = @($runtimeSmokeLines | Where-Object { $_.StartsWith('TORCH_VERSION=') } | Select-Object -Last 1)
    if ($torchLine.Count -ne 1) { throw 'Runtime smoke did not report the Torch version.' }
    $torchVersion = $torchLine[0].Substring('TORCH_VERSION='.Length)

    & (Join-Path $root 'tools\ffmpeg\ffmpeg.exe') -version *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Bundled FFmpeg verification failed.' }
    & (Join-Path $root 'tools\ffmpeg\ffprobe.exe') -version *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Bundled FFprobe verification failed.' }
    & (Join-Path $root 'tools\tesseract\tesseract.exe') --version *> $null
    if ($LASTEXITCODE -ne 0) { throw 'Bundled Tesseract verification failed.' }
    $languages = & (Join-Path $root 'tools\tesseract\tesseract.exe') --tessdata-dir (Join-Path $root 'tools\tesseract\tessdata') --list-langs 2>$null
    foreach ($language in @('eng','chi_sim','osd')) {
        if ($languages -notcontains $language) { throw "Bundled Tesseract language is missing: $language" }
    }

    Write-Host '========================================'
    Write-Host 'Mingjian Web setup complete'
    Write-Host 'Python: 3.11.9'
    Write-Host "Environment: $venv"
    Write-Host "Torch: $($torchVersion | Select-Object -Last 1)"
    Write-Host 'FFmpeg: OK'
    Write-Host 'Tesseract: OK'
    Write-Host 'Models: integrity OK'
    Write-Host 'Offline wheels: OK'
    Write-Host '========================================'
    Write-Host 'Demo:   .\start-demo.ps1'
    Write-Host 'Online: .\start-online.ps1'
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
