[CmdletBinding()]
param([ValidateRange(1,30)][int]$GraceSeconds = 5)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
. (Join-Path $root 'scripts\release-common.ps1')

try {
    $services = @(
        [pscustomobject]@{ Port = 8000; Name = 'main' },
        [pscustomobject]@{ Port = 8003; Name = 'asr' },
        [pscustomobject]@{ Port = 8005; Name = 'tavily' },
        [pscustomobject]@{ Port = 8006; Name = 'deepseek' }
    )
    foreach ($service in $services) {
        if (-not (Test-LocalTcpPort -Port $service.Port)) {
            Write-Host "[$($service.Port)] already stopped"
            continue
        }
        $health = Get-MingjianHealth -Port $service.Port
        if (-not (Test-MingjianHealthIdentity -Service $service.Name -Health $health)) {
            Write-Warning "[$($service.Port)] unknown process, skipped"
            continue
        }
        $listenerResult = Get-ListeningProcessIds -Port $service.Port
        if ($listenerResult.Status -ne 'Found') {
            Write-Warning "[$($service.Port)] Mingjian identity confirmed, but listener PID status is $($listenerResult.Status); skipped"
            continue
        }
        $ids = @($listenerResult.ProcessIds | Where-Object { $_ -gt 0 })
        if ($ids.Count -eq 0) {
            Write-Warning "[$($service.Port)] No valid listener PID greater than zero; skipped"
            continue
        }
        foreach ($id in $ids) {
            Stop-Process -Id $id -ErrorAction Stop
        }
        $deadline = [DateTime]::UtcNow.AddSeconds($GraceSeconds)
        while ((Test-LocalTcpPort -Port $service.Port) -and [DateTime]::UtcNow -lt $deadline) {
            Start-Sleep -Milliseconds 250
        }
        if (Test-LocalTcpPort -Port $service.Port) {
            $remainingResult = Get-ListeningProcessIds -Port $service.Port
            if ($remainingResult.Status -ne 'Found') {
                Write-Warning "[$($service.Port)] Listener PID status after graceful stop is $($remainingResult.Status); force stop skipped"
                continue
            }
            $remaining = @($remainingResult.ProcessIds | Where-Object { $_ -gt 0 })
            foreach ($id in $remaining) { Stop-Process -Id $id -Force -ErrorAction Stop }
        }
        Write-Host "[$($service.Port)] stopped"
    }
    foreach ($service in $services) {
        if (Test-LocalTcpPort -Port $service.Port) {
            Write-Warning "[$($service.Port)] remains listening"
        } else {
            Write-Host "[$($service.Port)] confirmed stopped"
        }
    }
    exit 0
} catch {
    Write-Error $_.Exception.Message
    exit 1
}
