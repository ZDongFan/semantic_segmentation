# Windows 解释器发现；由 bat 入口调用，不依赖预装的独立 Python。
param([switch]$DiscoverOnly, [switch]$FunctionsOnly)
$ErrorActionPreference = 'Stop'

function Get-VersionKey([string]$Value) {
    $match = [regex]::Match($Value, '\d+(?:\.\d+)*')
    $parts = @(if ($match.Success) { $match.Value.Split('.') })
    $key = for ($i = 0; $i -lt 4; $i++) {
        if ($i -lt $parts.Count) { '{0:D8}' -f [int]$parts[$i] } else { '00000000' }
    }
    return $key -join '.'
}

function Get-QgisRoots {
    # 当前会话优先；其余安装合并后按数字版本排序。
    if ($script:OriginalOsgeoRoot) { $script:OriginalOsgeoRoot }
    if ($script:OriginalQgisPrefix) {
        $prefix = [IO.Path]::GetFullPath($script:OriginalQgisPrefix)
        $prefix
        Split-Path (Split-Path $prefix -Parent) -Parent
    }
    $installed = @()
    foreach ($registry in @(
        'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
        'HKLM:\Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*'
    )) {
        foreach ($entry in (Get-ItemProperty -Path $registry -ErrorAction SilentlyContinue)) {
            if ($entry.DisplayName -like '*QGIS*' -and $entry.InstallLocation) {
                $version = if ($entry.DisplayVersion) { $entry.DisplayVersion } else { $entry.DisplayName }
                $installed += [pscustomobject]@{ Path = $entry.InstallLocation; Version = Get-VersionKey $version }
            }
        }
    }
    foreach ($base in @($env:ProgramFiles, ${env:ProgramFiles(x86)})) {
        if (-not $base) { continue }
        foreach ($dir in (Get-ChildItem -Path (Join-Path $base 'QGIS *') -Directory -ErrorAction SilentlyContinue)) {
            $installed += [pscustomobject]@{ Path = $dir.FullName; Version = Get-VersionKey $dir.Name }
        }
    }
    $installed | Sort-Object Version -Descending | ForEach-Object { $_.Path }
    'C:\OSGeo4W'
    'C:\OSGeo4W64'
}

function Get-QgisCandidates {
    foreach ($root in (Get-QgisRoots)) {
        foreach ($file in (Get-ChildItem -Path (Join-Path $root 'apps\Python3*\python.exe') -File -ErrorAction SilentlyContinue |
                Sort-Object { Get-VersionKey $_.Directory.Name } -Descending)) {
            [pscustomobject]@{ Path = $file.FullName; Source = 'QGIS'; Root = $root }
        }
    }
}

function Get-StandaloneCandidates {
    $ErrorActionPreference = 'Continue'
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) {
        $path = & $launcher.Source -3 -c 'import sys; print(sys.executable)' 2>$null
        if ($LASTEXITCODE -eq 0 -and $path) {
            [pscustomobject]@{ Path = [string]($path | Select-Object -Last 1); Source = 'py -3'; Root = '' }
        }
    }
    foreach ($name in @('python3.exe', 'python.exe')) {
        foreach ($command in (Get-Command $name -All -CommandType Application -ErrorAction SilentlyContinue)) {
            [pscustomobject]@{ Path = $command.Source; Source = 'PATH'; Root = '' }
        }
    }
    foreach ($pattern in @(
        "$env:LOCALAPPDATA\Programs\Python\Python*\python.exe",
        "$env:ProgramFiles\Python*\python.exe",
        "${env:ProgramFiles(x86)}\Python*\python.exe",
        'C:\Python*\python.exe'
    )) {
        foreach ($file in (Get-ChildItem -Path $pattern -File -ErrorAction SilentlyContinue |
                Sort-Object { Get-VersionKey $_.Directory.Name } -Descending)) {
            [pscustomobject]@{ Path = $file.FullName; Source = 'standalone Python'; Root = '' }
        }
    }
}

function Test-Candidate($Candidate) {
    if (-not (Test-Path -LiteralPath $Candidate.Path -PathType Leaf)) {
        Write-Host "Candidate does not exist: $($Candidate.Path)"
        return $false
    }
    $candidatePath = [IO.Path]::GetFullPath($Candidate.Path)
    # 禁止将 OSGeo4W 启动包装器作为自动回退解释器。
    $wrapperRoot = Split-Path (Split-Path $candidatePath -Parent) -Parent
    $isWrapperRoot = Test-Path -LiteralPath (Join-Path $wrapperRoot 'apps')
    if ($Candidate.Source -ne 'SAM_PYTHON' -and $isWrapperRoot) {
        if ((Split-Path (Split-Path $candidatePath -Parent) -Leaf) -eq 'bin') { return $false }
    }
    if (-not $script:Seen.Add($candidatePath)) { return $false }
    $savedPath = $env:PATH
    try {
        if ($Candidate.Root) { $env:PATH = (Join-Path $Candidate.Root 'bin') + ';' + $savedPath }
        Write-Host "Checking $($Candidate.Source): $candidatePath"
        # 原生进程失败属于候选诊断，应允许继续尝试下一个候选。
        $ErrorActionPreference = 'Continue'
        & $candidatePath $script:Helper --probe --target $script:Target --link $script:Link | Out-Host
        $valid = $LASTEXITCODE -eq 0
        if ($valid) { $script:SelectedPath = $env:PATH }
        return $valid
    } catch {
        Write-Host "Candidate failed: $($_.Exception.Message)"
        return $false
    } finally {
        $env:PATH = $savedPath
    }
}

function Select-Python {
    if ($env:SAM_PYTHON) {
        $path = $env:SAM_PYTHON
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            $command = Get-Command $path -CommandType Application -ErrorAction SilentlyContinue
            if ($command) { $path = $command.Source }
        }
        $candidate = [pscustomobject]@{ Path = $path; Source = 'SAM_PYTHON'; Root = '' }
        if (Test-Candidate $candidate) { return $candidate }
        throw 'Invalid SAM_PYTHON; no automatic fallback is allowed.'
    }
    foreach ($candidate in (Get-QgisCandidates)) {
        if (Test-Candidate $candidate) { return $candidate }
    }
    Write-Host 'No usable QGIS Python. Checking standalone Python.'
    foreach ($candidate in (Get-StandaloneCandidates)) {
        if (Test-Candidate $candidate) { return $candidate }
    }
    throw 'No usable Python with venv and ensurepip. Set SAM_PYTHON to an available interpreter.'
}

if ($FunctionsOnly) { return }
try {
    $script:Link = Join-Path $PSScriptRoot 'venv'
    $script:Target = if ($env:SAM_VENV_DIR) { [IO.Path]::GetFullPath($env:SAM_VENV_DIR) } else {
        Join-Path $env:LOCALAPPDATA 'LCCRuntime\venv'
    }
    if (-not $DiscoverOnly -and $env:SAM_RECREATE -ne '1' -and
        ((Get-Item -LiteralPath $script:Link -Force -ErrorAction SilentlyContinue) -or
         (Get-Item -LiteralPath $script:Target -Force -ErrorAction SilentlyContinue))) {
        throw 'Existing runtime found. Set SAM_RECREATE=1 to rebuild.'
    }
    $script:OriginalOsgeoRoot = $env:OSGEO4W_ROOT
    $script:OriginalQgisPrefix = $env:QGIS_PREFIX_PATH
    foreach ($name in @('PYTHONHOME', 'PYTHONPATH', 'PYTHONUSERBASE', 'QGIS_PREFIX_PATH', 'VIRTUAL_ENV')) {
        [Environment]::SetEnvironmentVariable($name, $null, 'Process')
    }
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONUTF8 = '1'
    $env:PYTHONIOENCODING = 'utf-8:backslashreplace'
    $script:Helper = Join-Path $PSScriptRoot 'runtime_setup.py'
    $script:Seen = New-Object 'System.Collections.Generic.HashSet[string]' ([StringComparer]::OrdinalIgnoreCase)
    $selected = Select-Python
    Write-Host "Selected $($selected.Source): $($selected.Path)"
    if ($DiscoverOnly) { exit 0 }
    $env:PATH = $script:SelectedPath
    & $selected.Path $script:Helper --target $script:Target --link $script:Link --source $selected.Source
    exit $LASTEXITCODE
} catch {
    Write-Host "Runtime setup failed: $($_.Exception.Message)"
    exit 1
}

