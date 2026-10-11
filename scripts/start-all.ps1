$ErrorActionPreference = "Stop"

$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runner = Join-Path $PSScriptRoot "run-service.ps1"

function Get-DotEnvValue {
    param([string]$Name)

    $envFile = Join-Path $root ".env"
    if (-not (Test-Path $envFile)) {
        return $null
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

    return $null
}

function Test-LocalTcpPort {
    param(
        [int]$Port,
        [int]$TimeoutMilliseconds = 500
    )

    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        $connectTask = $client.ConnectAsync("127.0.0.1", $Port)
        if (-not $connectTask.Wait($TimeoutMilliseconds)) {
            return $false
        }

        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Start-MingJianService {
    param(
        [string]$Title,
        [int]$Port,
        [string]$Service
    )

    if (Test-LocalTcpPort $Port) {
        Write-Host "[$Port] 端口已监听，跳过启动：$Title"
        Write-Warning "请确认该端口属于预期的明鉴服务。"
        return
    }

    $process = Start-Process `
        -FilePath "powershell.exe" `
        -WorkingDirectory $root `
        -ArgumentList @(
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            $runner,
            "-Service",
            $Service
        ) `
        -PassThru

    Start-Sleep -Seconds 1
    $process.Refresh()

    if ($process.HasExited) {
        Write-Error "[$Port] 启动失败：$Title 子进程已退出，ExitCode=$($process.ExitCode)" -ErrorAction Continue
        return
    }

    Write-Host "[$Port] 启动进程已创建：$Title"
}

if (Test-Path Env:DEMO_MODE) {
    $demoModeValue = $env:DEMO_MODE
} else {
    $demoModeValue = Get-DotEnvValue "DEMO_MODE"
    if ($null -ne $demoModeValue) {
        $env:DEMO_MODE = $demoModeValue
    }
}

$demoMode = $false
if ($null -ne $demoModeValue) {
    switch ($demoModeValue.Trim().ToLowerInvariant()) {
        "true" { $demoMode = $true }
        "false" { $demoMode = $false }
        default { throw "DEMO_MODE must be true or false." }
    }
}

Start-MingJianService "05-ASR-8003" 8003 "asr"

if ($demoMode) {
    Write-Host "Demo mode enabled."
    Write-Host "Skipping Tavily service (8005)."
    Write-Host "Skipping DeepSeek service (8006)."
} else {
    Start-MingJianService "03-Tavily-8005"   8005 "tavily"
    Start-MingJianService "01-DeepSeek-8006" 8006 "deepseek"
}

Start-MingJianService "04-Main-8000"     8000 "main"

Write-Host ""
Write-Host "明鉴服务启动命令已执行。"
