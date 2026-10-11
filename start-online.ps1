[CmdletBinding()]
param([switch]$NoBrowser, [ValidateRange(1,600)][int]$HealthTimeoutSeconds = 120)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'scripts\release-common.ps1')

try {
    [void](Assert-ReleaseVenvPython -ProjectRoot $root)
    $envTavily = (Test-Path Env:TAVILY_API_KEY) -and -not [string]::IsNullOrWhiteSpace($env:TAVILY_API_KEY)
    $envDeepSeek = (Test-Path Env:DEEPSEEK_API_KEY) -and -not [string]::IsNullOrWhiteSpace($env:DEEPSEEK_API_KEY)
    $fileTavily = $null -ne (Get-ReleaseDotEnvValue -ProjectRoot $root -Name 'TAVILY_API_KEY')
    $fileDeepSeek = $null -ne (Get-ReleaseDotEnvValue -ProjectRoot $root -Name 'DEEPSEEK_API_KEY')
    Write-Host "TAVILY_API_KEY_PRESENT=$($envTavily -or $fileTavily)"
    Write-Host "DEEPSEEK_API_KEY_PRESENT=$($envDeepSeek -or $fileDeepSeek)"
    if (-not ($envTavily -or $fileTavily) -or -not ($envDeepSeek -or $fileDeepSeek)) {
        throw 'Online mode requires both TAVILY_API_KEY and DEEPSEEK_API_KEY.'
    }
    # The stable Worker launcher intentionally reloads secrets from .env after clearing inherited keys.
    if (-not $fileTavily -or -not $fileDeepSeek) {
        throw 'The current stable Worker launcher requires both keys in the project .env file.'
    }
    $services = @(
        [pscustomobject]@{ Port = 8000; Name = 'main' },
        [pscustomobject]@{ Port = 8003; Name = 'asr' },
        [pscustomobject]@{ Port = 8005; Name = 'tavily' },
        [pscustomobject]@{ Port = 8006; Name = 'deepseek' }
    )
    foreach ($item in $services) {
        Assert-PortAvailableOrMingjian -Port $item.Port -Service $item.Name
    }
    Set-ReleaseRuntimeEnvironment -ProjectRoot $root -DemoMode 'false'
    & (Join-Path $root 'scripts\start-all.ps1')
    [void](Wait-MingjianHealth -Port 8003 -Service 'asr' -TimeoutSeconds $HealthTimeoutSeconds)
    [void](Wait-MingjianHealth -Port 8005 -Service 'tavily' -TimeoutSeconds $HealthTimeoutSeconds -RequireConfigured)
    [void](Wait-MingjianHealth -Port 8006 -Service 'deepseek' -TimeoutSeconds $HealthTimeoutSeconds -RequireConfigured)
    [void](Wait-MingjianHealth -Port 8000 -Service 'main' -TimeoutSeconds $HealthTimeoutSeconds)
    $url = 'http://127.0.0.1:8000/'
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host 'Mingjian Web Online READY'
    Write-Host $url
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
