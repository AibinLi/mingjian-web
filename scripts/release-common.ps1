Set-StrictMode -Version Latest

function Invoke-ReleasePythonProbe {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$LauncherArgs = @(),
        [string]$Label = $Executable
    )

    $probeScript = @'
import json
import os
import platform
import struct
import sys

print(json.dumps({
    'version': platform.python_version(),
    'implementation': platform.python_implementation(),
    'bits': struct.calcsize('P') * 8,
    'machine': platform.machine(),
    'executable': sys.executable,
    'is_conda': os.path.isdir(os.path.join(sys.prefix, 'conda-meta')) or 'conda' in sys.version.lower(),
}))
'@

    try {
        $output = @($probeScript | & $Executable @LauncherArgs - 2>&1)
        $exitCode = $LASTEXITCODE
    } catch {
        return [pscustomobject]@{
            Status = 'ProbeError'; Executable = $Executable; LauncherArgs = @($LauncherArgs)
            Label = $Label; Info = $null; ErrorType = 'LaunchFailed'
        }
    }

    if ($exitCode -ne 0) {
        return [pscustomobject]@{
            Status = 'ProbeError'; Executable = $Executable; LauncherArgs = @($LauncherArgs)
            Label = $Label; Info = $null; ErrorType = "NonZeroExit:$exitCode"
        }
    }

    $jsonLine = @($output | ForEach-Object { [string]$_ } | Where-Object { $_.Trim().StartsWith('{') -and $_.Trim().EndsWith('}') } | Select-Object -Last 1)
    if ($jsonLine.Count -ne 1) {
        return [pscustomobject]@{
            Status = 'ProbeError'; Executable = $Executable; LauncherArgs = @($LauncherArgs)
            Label = $Label; Info = $null; ErrorType = 'MissingJsonOutput'
        }
    }

    try {
        $info = $jsonLine[0] | ConvertFrom-Json
        foreach ($name in @('version','implementation','bits','machine','executable','is_conda')) {
            if ($null -eq $info.PSObject.Properties[$name]) { throw "Missing probe field: $name" }
        }
    } catch {
        return [pscustomobject]@{
            Status = 'ProbeError'; Executable = $Executable; LauncherArgs = @($LauncherArgs)
            Label = $Label; Info = $null; ErrorType = 'InvalidJsonOutput'
        }
    }

    $status = if (Test-ReleasePythonInfo -Info $info) { 'Compatible' } else { 'Incompatible' }
    return [pscustomobject]@{
        Status = $status; Executable = $Executable; LauncherArgs = @($LauncherArgs)
        Label = $Label; Info = $info; ErrorType = $null
    }
}

function Get-ReleasePythonInfo {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [string[]]$LauncherArgs = @()
    )

    $result = Invoke-ReleasePythonProbe -Executable $Executable -LauncherArgs $LauncherArgs
    if ($result.Status -eq 'ProbeError') { return $null }
    return $result.Info
}

function Test-ReleasePythonInfo {
    param($Info)

    if ($null -eq $Info) { return $false }
    $machine = [string]$Info.machine
    return (
        $Info.version -eq "3.11.9" -and
        $Info.implementation -eq "CPython" -and
        [int]$Info.bits -eq 64 -and
        $Info.is_conda -ne $true -and
        ($machine -match '^(AMD64|x86_64)$')
    )
}

function Get-ReleasePythonCandidates {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $candidates = New-Object System.Collections.ArrayList
    $seen = @{}

    foreach ($command in @(Get-Command python -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ($command.Source -and -not $seen.ContainsKey($command.Source)) {
            $seen[$command.Source] = $true
            [void]$candidates.Add([pscustomobject]@{ Executable = $command.Source; LauncherArgs = @(); Label = $command.Source })
        }
    }

    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue
    if ($py) {
        [void]$candidates.Add([pscustomobject]@{ Executable = $py.Source; LauncherArgs = @('-3.11'); Label = "$($py.Source) -3.11" })
    }

    $paths = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'MingjianWeb\Python3119\python.exe'),
        (Join-Path $env:ProgramFiles 'Python311\python.exe')
    )
    foreach ($path in $paths) {
        if ((Test-Path -LiteralPath $path -PathType Leaf) -and -not $seen.ContainsKey($path)) {
            $seen[$path] = $true
            [void]$candidates.Add([pscustomobject]@{ Executable = $path; LauncherArgs = @(); Label = $path })
        }
    }
    return @($candidates)
}

function Find-ReleasePython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    foreach ($candidate in @(Get-ReleasePythonCandidates -ProjectRoot $ProjectRoot)) {
        $info = Get-ReleasePythonInfo -Executable $candidate.Executable -LauncherArgs $candidate.LauncherArgs
        if (Test-ReleasePythonInfo -Info $info) {
            return [pscustomobject]@{
                Executable = $candidate.Executable
                LauncherArgs = @($candidate.LauncherArgs)
                Label = $candidate.Label
                Info = $info
            }
        }
    }
    return $null
}

function Get-ReleasePythonDiscovery {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $results = @()
    $compatible = $null
    foreach ($candidate in @(Get-ReleasePythonCandidates -ProjectRoot $ProjectRoot)) {
        $result = Invoke-ReleasePythonProbe -Executable $candidate.Executable -LauncherArgs $candidate.LauncherArgs -Label $candidate.Label
        $results += $result
        if ($null -eq $compatible -and $result.Status -eq 'Compatible') {
            $compatible = [pscustomobject]@{
                Executable = $candidate.Executable
                LauncherArgs = @($candidate.LauncherArgs)
                Label = $candidate.Label
                Info = $result.Info
            }
        }
    }
    return [pscustomobject]@{
        CompatibleCandidate = $compatible
        Results = @($results)
        HasProbeError = @($results | Where-Object { $_.Status -eq 'ProbeError' }).Count -gt 0
    }
}

function Test-ReleaseInstallerBootstrapAllowed {
    param([Parameter(Mandatory = $true)]$Discovery)
    return $null -eq $Discovery.CompatibleCandidate -and -not $Discovery.HasProbeError
}

function Assert-ReleaseVenvPython {
    param([Parameter(Mandatory = $true)][string]$ProjectRoot)

    $python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
        throw "Run .\setup.ps1 first. Missing: $python"
    }
    $info = Get-ReleasePythonInfo -Executable $python
    if (-not (Test-ReleasePythonInfo -Info $info)) {
        throw "The project .venv is not CPython 3.11.9 x64. Run .\setup.ps1 -RecreateVenv."
    }
    return $python
}

function Assert-RequiredPath {
    param([Parameter(Mandatory = $true)][string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        throw "Required release resource is missing: $Path"
    }
}

function Test-ReleaseHashManifest {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$ManifestPath,
        [string]$RelativeBase = ''
    )

    Assert-RequiredPath -Path $ManifestPath
    $base = [IO.Path]::GetFullPath((Join-Path $ProjectRoot $RelativeBase))
    $project = [IO.Path]::GetFullPath($ProjectRoot).TrimEnd('\') + '\'
    $count = 0
    foreach ($rawLine in Get-Content -LiteralPath $ManifestPath -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#')) { continue }
        if ($line -notmatch '^([0-9a-fA-F]{64})\s{2}(.+)$') {
            throw "Invalid hash manifest line in $ManifestPath"
        }
        $expected = $matches[1].ToLowerInvariant()
        $relative = $matches[2].Replace('/', '\')
        $file = [IO.Path]::GetFullPath((Join-Path $base $relative))
        if (-not $file.StartsWith($project, [StringComparison]::OrdinalIgnoreCase)) {
            throw "Manifest path escapes the project root: $relative"
        }
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) {
            throw "Manifest file is missing: $relative"
        }
        $actual = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($actual -ne $expected) {
            throw "SHA-256 mismatch: $relative"
        }
        $count++
    }
    if ($count -eq 0) { throw "Hash manifest is empty: $ManifestPath" }
    return $count
}

function Get-ReleaseDotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][string]$Name
    )

    $path = Join-Path $ProjectRoot '.env'
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $null }
    foreach ($rawLine in Get-Content -LiteralPath $path) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith('#') -or -not $line.Contains('=')) { continue }
        $parts = $line -split '=', 2
        if ($parts[0].Trim() -ne $Name) { continue }
        $value = $parts[1].Trim()
        if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        if ([string]::IsNullOrWhiteSpace($value)) { return $null }
        return $value
    }
    return $null
}

function Test-LocalTcpPort {
    param([int]$Port, [int]$TimeoutMilliseconds = 500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $task = $client.ConnectAsync('127.0.0.1', $Port)
        if (-not $task.Wait($TimeoutMilliseconds)) { return $false }
        return $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-MingjianHealth {
    param([Parameter(Mandatory = $true)][int]$Port)
    try {
        return Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Method Get -TimeoutSec 3
    } catch {
        return $null
    }
}

function Test-MingjianHealthIdentity {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('main','asr','tavily','deepseek')][string]$Service,
        $Health,
        [switch]$RequireConfigured
    )
    if ($null -eq $Health) { return $false }
    $statusProperty = $Health.PSObject.Properties['status']
    if ($null -eq $statusProperty -or $statusProperty.Value -ne 'ok') { return $false }
    switch ($Service) {
        'main' {
            $property = $Health.PSObject.Properties['service']
            return $null -ne $property -and $property.Value -eq 'mingjian-web-api'
        }
        'asr' {
            $asrProperty = $Health.PSObject.Properties['asr']
            if ($null -eq $asrProperty -or $null -eq $asrProperty.Value) { return $false }
            $nameProperty = $asrProperty.Value.PSObject.Properties['name']
            return $null -ne $nameProperty -and $nameProperty.Value -eq 'OpenAI Whisper base'
        }
        'tavily' {
            $serviceProperty = $Health.PSObject.Properties['service']
            if ($null -eq $serviceProperty -or $serviceProperty.Value -ne 'mingjian-tavily-worker') { return $false }
            if ($RequireConfigured) {
                $retrieval = $Health.PSObject.Properties['retrieval_enabled']
                $configured = $Health.PSObject.Properties['tavily_configured']
                return $null -ne $retrieval -and $retrieval.Value -eq $true -and $null -ne $configured -and $configured.Value -eq $true
            }
            return $true
        }
        'deepseek' {
            $configured = $Health.PSObject.Properties['deepseek_configured']
            $tavily = $Health.PSObject.Properties['tavily_configured']
            $identity = $null -ne $configured -and $null -ne $tavily
            if (-not $identity) { return $false }
            if ($RequireConfigured) { return $configured.Value -eq $true }
            return $true
        }
    }
}

function Assert-PortAvailableOrMingjian {
    param([int]$Port, [ValidateSet('main','asr','tavily','deepseek')][string]$Service)
    if (-not (Test-LocalTcpPort -Port $Port)) { return }
    $health = Get-MingjianHealth -Port $Port
    if (-not (Test-MingjianHealthIdentity -Service $Service -Health $health)) {
        throw "Port $Port is occupied by an unknown process."
    }
}

function Wait-MingjianHealth {
    param(
        [int]$Port,
        [ValidateSet('main','asr','tavily','deepseek')][string]$Service,
        [int]$TimeoutSeconds = 120,
        [switch]$RequireConfigured
    )
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $health = Get-MingjianHealth -Port $Port
        if (Test-MingjianHealthIdentity -Service $Service -Health $health -RequireConfigured:$RequireConfigured) {
            return $health
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTime]::UtcNow -lt $deadline)
    throw "Mingjian $Service health check timed out on port $Port after $TimeoutSeconds seconds."
}

function Get-ListeningProcessIdParseResult {
    param(
        [Parameter(Mandatory = $true)][int]$Port,
        [Parameter(Mandatory = $true)][AllowEmptyCollection()][string[]]$Lines
    )

    $ids = @()
    $parseErrors = @()
    foreach ($line in $Lines) {
        $match = [regex]::Match($line, '^\s*TCP\s+(?<local>\S+)\s+\S+\s+LISTENING(?:\s+(?<pid>\S+))?\s*$')
        if (-not $match.Success) { continue }

        $localEndpoint = $match.Groups['local'].Value
        $endpointMatch = [regex]::Match($localEndpoint, ':(?<port>\d+)$')
        if (-not $endpointMatch.Success -or [int]$endpointMatch.Groups['port'].Value -ne $Port) { continue }

        $parsedPid = 0
        if (-not [int]::TryParse($match.Groups['pid'].Value, [ref]$parsedPid) -or $parsedPid -le 0) {
            $parseErrors += $line
            continue
        }
        $ids += $parsedPid
    }

    $uniqueIds = @($ids | Sort-Object -Unique)
    if ($parseErrors.Count -gt 0) {
        return [pscustomobject]@{ Status = 'ParseError'; ProcessIds = $uniqueIds; ErrorCount = $parseErrors.Count }
    }
    if ($uniqueIds.Count -eq 0) {
        return [pscustomobject]@{ Status = 'NoListener'; ProcessIds = @(); ErrorCount = 0 }
    }
    return [pscustomobject]@{ Status = 'Found'; ProcessIds = $uniqueIds; ErrorCount = 0 }
}

function Get-ListeningProcessIds {
    param([Parameter(Mandatory = $true)][int]$Port)
    $lines = @(& "$env:SystemRoot\System32\netstat.exe" -ano -p TCP | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
    return Get-ListeningProcessIdParseResult -Port $Port -Lines $lines
}

function Set-ReleaseRuntimeEnvironment {
    param(
        [Parameter(Mandatory = $true)][string]$ProjectRoot,
        [Parameter(Mandatory = $true)][ValidateSet('true','false')][string]$DemoMode
    )
    $env:DEMO_MODE = $DemoMode
    $env:HF_HUB_OFFLINE = '1'
    $env:TRANSFORMERS_OFFLINE = '1'
    $env:MINGJIAN_TESSERACT_PATH = Join-Path $ProjectRoot 'tools\tesseract\tesseract.exe'
    $env:TESSDATA_PREFIX = Join-Path $ProjectRoot 'tools\tesseract\tessdata'
    $parts = @(
        (Join-Path $ProjectRoot '.venv\Scripts'),
        (Join-Path $ProjectRoot 'tools\ffmpeg'),
        (Join-Path $ProjectRoot 'tools\tesseract')
    )
    $env:Path = ($parts -join ';') + ';' + $env:Path
}
