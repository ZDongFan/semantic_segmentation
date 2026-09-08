@echo off
setlocal EnableExtensions DisableDelayedExpansion
rem 批处理内联执行系统 PowerShell，不依赖 QGIS 或系统 Python。
set "LCC_SETUP_BAT=%~f0"
set "LCC_SETUP_DIR=%~dp0"
set "LCC_POWERSHELL=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if exist "%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe" set "LCC_POWERSHELL=%SystemRoot%\Sysnative\WindowsPowerShell\v1.0\powershell.exe"
"%LCC_POWERSHELL%" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$text=[IO.File]::ReadAllText($env:LCC_SETUP_BAT,[Text.Encoding]::UTF8); & ([scriptblock]::Create($text.Substring($text.LastIndexOf('# POWERSHELL_BEGIN'))))"
set "LCC_EXIT=%errorlevel%"
if /I "%~1"=="--non-interactive" exit /b %LCC_EXIT%
pause
exit /b %LCC_EXIT%
# POWERSHELL_BEGIN
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$lock = $null
$logPath = $null
$exitCode = 1
function Write-Log([string]$text) {
    $safe = [regex]::Replace($text, '(?i)(https?|socks5h?|socks4)://[^\s<>"'']+', '[redacted URL]')
    [Console]::WriteLine($safe)
    if ($script:logPath) { [IO.File]::AppendAllText($script:logPath, $safe + [Environment]::NewLine, [Text.Encoding]::UTF8) }
}
function Get-ArchiveHash([string]$path) {
    $stream = [IO.File]::OpenRead($path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return [BitConverter]::ToString($hasher.ComputeHash($stream)).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}
function Resolve-WindowsPlatform([string]$arch, [bool]$is64BitOS) {
    if (-not $is64BitOS) { throw '32-bit systems are not supported by current PyTorch/SAM2 dependencies.' }
    if ($arch -ne 'AMD64') { throw "当前安装脚本未提供该平台组合: Windows / $arch" }
    return 'Windows/x86_64'
}
function Test-Cancel {
    if ($env:LCC_CANCEL_FILE -and (Test-Path -LiteralPath $env:LCC_CANCEL_FILE)) { throw 'Installation cancelled by user; incomplete directories retained.' }
}
try {
    $root = Join-Path $env:LOCALAPPDATA 'LCCRuntime'
    [IO.Directory]::CreateDirectory((Join-Path $root 'logs')) | Out-Null
    $script:logPath = Join-Path $root ('logs\install-' + (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '-' + $PID + '.log')
    $env:LCC_LOG_PATH = $script:logPath
    $env:LCC_LOG_CAPTURED = '1'
    Write-Log "Log: $script:logPath"
    # 系统架构来自操作系统注册表，不以当前 PowerShell 的位数推断。
    $arch = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').PROCESSOR_ARCHITECTURE
    Write-Log "[stage] Platform: Windows / $arch"
    Resolve-WindowsPlatform $arch ([Environment]::Is64BitOperatingSystem) | Out-Null
    if ($env:SAM_PYTHON) { Write-Log 'SAM_PYTHON is no longer used. This installer downloads standalone CPython 3.12.12.' }
    if ($env:SAM_RECREATE) { Write-Log 'SAM_RECREATE is no longer supported. Existing environments are checked only.' }
    if ($env:SAM_VENV_DIR) { throw 'Windows stores the venv in %LOCALAPPDATA%\LCCRuntime\venv; SAM_VENV_DIR applies only to Linux/macOS.' }
    # 文件句柄独占锁在退出或取消后由系统释放；不触碰旧状态文件。
    $lockPath = Join-Path $root 'standalone-3.12.12.lock'
    $lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    $link = Join-Path $env:LCC_SETUP_DIR 'venv'
    $target = Join-Path $root 'venv'
    $base = Join-Path $root 'python-3.12.12-20251014'
    $python = Join-Path $base 'python\python.exe'
    $helper = Join-Path $env:LCC_SETUP_DIR 'runtime_setup.py'
    $nativeRoot = Join-Path $env:SystemRoot 'System32'
    $tar = Join-Path $nativeRoot 'tar.exe'
    $curl = Join-Path $nativeRoot 'curl.exe'
    # 引导阶段即清除 QGIS 注入，防止独立解释器启动时加载错误 DLL。
    $qgisRoots = @($env:QGIS_PREFIX_PATH, $env:OSGEO4W_ROOT) | Where-Object { $_ }
    $env:PATH = (($env:PATH -split ';') | Where-Object {
        $entry = $_
        $entry -and $entry -notmatch '(?i)qgis|osgeo4w' -and -not ($qgisRoots | Where-Object { $entry.StartsWith($_, [StringComparison]::OrdinalIgnoreCase) })
    }) -join ';'
    foreach ($key in @(Get-ChildItem Env:)) {
        if ($key.Name -match '^(PYTHON|GDAL_|PROJ_|DYLD_)|^(QGIS_PREFIX_PATH|OSGEO4W_ROOT|VIRTUAL_ENV|GEOTIFF_CSV|LD_LIBRARY_PATH|LD_PRELOAD|QT_PLUGIN_PATH|QT_QPA_PLATFORM_PLUGIN_PATH)$') {
            [Environment]::SetEnvironmentVariable($key.Name, $null, 'Process')
        }
    }
    $env:PYTHONUTF8 = '1'
    $env:PYTHONNOUSERSITE = '1'
    $env:PYTHONUNBUFFERED = '1'
    $env:PYTHONIOENCODING = 'utf-8:backslashreplace'
    $permissionProbe = Join-Path $env:LCC_SETUP_DIR ('.write-test-' + $PID)
    $probeHandle = [IO.File]::Open($permissionProbe, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write)
    $probeHandle.Dispose()
    [IO.File]::Delete($permissionProbe)
    Test-Cancel
    if (Get-Item -LiteralPath $target -Force -ErrorAction SilentlyContinue) {
        $existingPython = Join-Path $target 'Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $existingPython -PathType Leaf)) { throw "Incomplete venv retained: $target" }
        $global:LASTEXITCODE = 1
        $ErrorActionPreference = 'Continue'
        & $existingPython $helper --target $target --link $link --verify-only 2>&1 | ForEach-Object { Write-Log "$_" }
        $exitCode = $global:LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    } else {
        if (Get-Item -LiteralPath $link -Force -ErrorAction SilentlyContinue) { throw "Fixed entry already exists: $link" }
        $sha = '2d670beb3b930d30e3a13cc909923a001dbdfcb5537692d5da40b6b41643ce1c'
        if (-not (Get-Item -LiteralPath $base -Force -ErrorAction SilentlyContinue)) {
            foreach ($tool in @($tar, $curl)) { if (-not (Test-Path -LiteralPath $tool)) { throw "Required system tool missing: $tool" } }
            $asset = 'cpython-3.12.12%2B20251014-x86_64-pc-windows-msvc-install_only.tar.gz'
            $url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20251014/' + $asset
            $sha = '2d670beb3b930d30e3a13cc909923a001dbdfcb5537692d5da40b6b41643ce1c'
            $cacheDir = Join-Path $root 'downloads'
            [IO.Directory]::CreateDirectory($cacheDir) | Out-Null
            $cache = Join-Path $cacheDir ($sha + '.tar.gz')
            if (-not (Test-Path -LiteralPath $cache)) {
                Write-Log '[stage] Downloading standalone CPython 3.12.12 / 20251014'
                $part = $cache + '.part-' + $PID
                $start = New-Object Diagnostics.ProcessStartInfo
                $start.FileName = $curl
                $start.Arguments = '-fL --silent --show-error --retry 2 --connect-timeout 30 --max-time 900 --proto "=https" --tlsv1.2 --output "' + $part + '" "' + $url + '"'
                $start.UseShellExecute = $false
                $start.CreateNoWindow = $true
                $start.RedirectStandardError = $true
                $download = [Diagnostics.Process]::Start($start)
                $errorTask = $download.StandardError.ReadToEndAsync()
                try {
                    while (-not $download.WaitForExit(200)) { Test-Cancel }
                    $errorText = $errorTask.Result
                    if ($errorText) { Write-Log $errorText }
                    if ($download.ExitCode -ne 0) { throw "curl failed (exit $($download.ExitCode)); partial download: $part" }
                } finally {
                    if (-not $download.HasExited) { $download.Kill(); $download.WaitForExit() }
                    $download.Dispose()
                }
                if ((Get-ArchiveHash $part) -ne $sha) { throw "SHA-256 mismatch: $part" }
                [IO.File]::Move($part, $cache)
            }
            if ((Get-ArchiveHash $cache) -ne $sha) { throw "SHA-256 mismatch: $cache" }
            Write-Log '[stage] SHA-256 verified; checking archive paths'
            $names = @(& $tar -tzf $cache 2>&1)
            if ($LASTEXITCODE -ne 0) { throw ($names -join "`n") }
            foreach ($name in $names) {
                if ($name -notmatch '^python/' -or $name -match '(^|/)\.\.(/|$)|[\\:]') { throw "Unsafe archive path: $name" }
            }
            $details = @(& $tar -tvzf $cache 2>&1)
            if ($LASTEXITCODE -ne 0) { throw ($details -join "`n") }
            foreach ($entry in $details) {
                if ($entry -notmatch '^[-d]') { throw "Unsupported archive member: $entry" }
            }
            Test-Cancel
            [IO.Directory]::CreateDirectory($base) | Out-Null
            Write-Log "[stage] Extracting Python to final location: $base"
            & $tar -xzf $cache -C $base 2>&1 | ForEach-Object { Write-Log "$_" }
            if ($LASTEXITCODE -ne 0) { throw "tar failed (exit $LASTEXITCODE); incomplete Python retained: $base" }
            [IO.File]::WriteAllText((Join-Path $base '.verified-archive'), $sha)
        }
        if (-not (Test-Path -LiteralPath (Join-Path $base '.verified-archive')) -or [IO.File]::ReadAllText((Join-Path $base '.verified-archive')) -ne $sha) { throw "Incomplete/unrecognized Python directory retained: $base" }
        Write-Log "[stage] Starting standalone Python: $python"
        $global:LASTEXITCODE = 1
        $ErrorActionPreference = 'Continue'
        & $python $helper --target $target --link $link 2>&1 | ForEach-Object { Write-Log "$_" }
        $exitCode = $global:LASTEXITCODE
        $ErrorActionPreference = 'Stop'
    }
    if ($exitCode -ne 0) { Write-Log "Installation stopped (exit $exitCode). Log: $script:logPath" }
} catch {
    Write-Log ($_.Exception.ToString() + "`n" + $_.ScriptStackTrace)
    Write-Log "Installation stopped (exit 1). Log: $script:logPath"
    $exitCode = 1
} finally {
    if ($lock) { $lock.Dispose() }
}
exit $exitCode
