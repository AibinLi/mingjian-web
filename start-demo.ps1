[CmdletBinding()]
param([switch]$NoBrowser, [ValidateRange(1,600)][int]$HealthTimeoutSeconds = 120)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'scripts\release-common.ps1')

try {
    [void](Assert-ReleaseVenvPython -ProjectRoot $root)
    $requiredServices = @(
        [pscustomobject]@{ Port = 8000; Name = 'main' },
        [pscustomobject]@{ Port = 8003; Name = 'asr' }
    )
    foreach ($item in $requiredServices) {
        Assert-PortAvailableOrMingjian -Port $item.Port -Service $item.Name
    }
    $onlineServices = @(
        [pscustomobject]@{ Port = 8005; Name = 'tavily' },
        [pscustomobject]@{ Port = 8006; Name = 'deepseek' }
    )
    foreach ($item in $onlineServices) {
        if (Test-LocalTcpPort -Port $item.Port) {
            $health = Get-MingjianHealth -Port $item.Port
            if (Test-MingjianHealthIdentity -Service $item.Name -Health $health) {
                throw "Mingjian online workers are already running on port $($item.Port). Run .\stop-all.ps1 first."
            }
            throw "Port $($item.Port) is occupied by an unknown process."
        }
    }
    Set-ReleaseRuntimeEnvironment -ProjectRoot $root -DemoMode 'true'
    & (Join-Path $root 'scripts\start-all.ps1')
    [void](Wait-MingjianHealth -Port 8003 -Service 'asr' -TimeoutSeconds $HealthTimeoutSeconds)
    [void](Wait-MingjianHealth -Port 8000 -Service 'main' -TimeoutSeconds $HealthTimeoutSeconds)
    $url = 'http://127.0.0.1:8000/'
    if (-not $NoBrowser) { Start-Process $url }
    Write-Host 'Mingjian Web Demo READY'
    Write-Host $url
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
