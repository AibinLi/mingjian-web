param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("asr", "tavily", "deepseek", "main")]
    [string]$Service
)

$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $root

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
$pythonExecutable = $null
$pythonLauncherArgs = @()

function Test-PythonCandidate {
    param(
        [string]$Executable,
        [string[]]$LauncherArgs = @()
    )

    try {
        & $Executable @LauncherArgs --version *> $null
        return $LASTEXITCODE -eq 0
    } catch {
        return $false
    }
}

if (Test-Path $venvPython) {
    if (Test-PythonCandidate $venvPython) {
        $pythonExecutable = $venvPython
    } else {
        Write-Warning "Project .venv Python exists but is not executable. Falling back to system Python."
    }
}

if (-not $pythonExecutable) {
    $pythonCommands = @(Get-Command python -CommandType Application -ErrorAction SilentlyContinue)
    $seenPythonCandidates = @{}

    foreach ($pythonCommand in $pythonCommands) {
        $candidate = $pythonCommand.Source
        if (-not $candidate -or $seenPythonCandidates.ContainsKey($candidate)) {
            continue
        }

        $seenPythonCandidates[$candidate] = $true
        if (Test-PythonCandidate $candidate) {
            $pythonExecutable = $candidate
            Write-Warning "Using Python from PATH: $pythonExecutable"
            break
        }

        Write-Warning "Python candidate is not usable: $candidate"
    }
}

if (-not $pythonExecutable) {
    $pyCommand = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    $pyLauncherArgs = @("-3")

    if ($pyCommand -and (Test-PythonCandidate $pyCommand.Source $pyLauncherArgs)) {
        $pythonExecutable = $pyCommand.Source
        $pythonLauncherArgs = $pyLauncherArgs
        Write-Warning "Using Python launcher: $pythonExecutable -3"
    } elseif ($pyCommand) {
        Write-Warning "The Python launcher was found but py -3 is not usable."
    }
}

if (-not $pythonExecutable) {
    throw @"
No usable Python interpreter was found.
The project .venv may be broken or machine-specific.
Install Python and recreate the project .venv.
"@
}

# 所有服务默认都拿不到外部 API Key
Remove-Item Env:DEEPSEEK_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:TAVILY_API_KEY -ErrorAction SilentlyContinue

function Get-LocalSecret {
    param([string]$Name)

    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) {
        throw ".env 不存在：$envFile"
    }

    foreach ($rawLine in Get-Content $envFile) {
        $line = $rawLine.Trim()

        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            continue
        }

        $parts = $line -split "=", 2

        if ($parts[0].Trim() -ne $Name) {
            continue
        }

        $value = $parts[1].Trim()

        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }

        return $value
    }

    throw "$Name 未配置"
}

switch ($Service) {

    "asr" {
        $Host.UI.RawUI.WindowTitle = "05-ASR-8003"

        $env:MINGJIAN_DATA_DIR = Join-Path $root "backend\data"

        & $pythonExecutable @pythonLauncherArgs `
            -m backend.app.asr_worker_main
    }

    "tavily" {
        $Host.UI.RawUI.WindowTitle = "03-Tavily-8005"

        $env:ENABLE_RETRIEVAL = "true"
        $env:TAVILY_API_KEY = Get-LocalSecret "TAVILY_API_KEY"

        & $pythonExecutable @pythonLauncherArgs `
            -m backend.app.tavily_worker_main
    }

    "deepseek" {
        $Host.UI.RawUI.WindowTitle = "01-DeepSeek-8006"

        $env:DEEPSEEK_API_KEY = Get-LocalSecret "DEEPSEEK_API_KEY"
        $env:MINGJIAN_LLM_WORKER_PORT = "8006"

        & $pythonExecutable @pythonLauncherArgs `
            -m backend.app.deepseek_worker
    }

    "main" {
        $Host.UI.RawUI.WindowTitle = "04-Main-8000"

        $env:ENABLE_ASR = "true"
        $env:ENABLE_OCR = "true"
        $env:ENABLE_FACE = "false"
        $env:ENABLE_SYNC = "false"
        $env:ENABLE_RETRIEVAL = "true"
        $env:ENABLE_LLM = "true"

        $env:MINGJIAN_ASR_WORKER_URL = "http://127.0.0.1:8003"
        $env:MINGJIAN_RETRIEVAL_WORKER_URL = "http://127.0.0.1:8005"
        $env:MINGJIAN_LLM_ANALYSIS_URL = "http://127.0.0.1:8006"

        $env:MINGJIAN_RETENTION_SECONDS = "86400"
        $env:MINGJIAN_JOB_TIMEOUT_SECONDS = "600"

        & $pythonExecutable @pythonLauncherArgs `
            -m uvicorn backend.app.main:app `
            --host 0.0.0.0 `
            --port 8000
    }
}
