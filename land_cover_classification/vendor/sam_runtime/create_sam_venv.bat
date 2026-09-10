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
    [Console]::Out.Flush()
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

function Open-BootstrapLock([string]$path) {
    return [IO.File]::Open($path, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
}
function Assert-DownloadFile([string]$path) {
    $item = Get-Item -LiteralPath $path -Force -ErrorAction SilentlyContinue
    if ($item -and ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint))) {
        throw "下载路径必须是普通文件，已保留: $path"
    }
}
function Get-DownloadSize([string]$path) {
    if (-not [IO.File]::Exists($path)) { return [long]0 }
    # 从共享句柄获取实时长度，避免 curl 写入期间目录元数据尚未刷新。
    $handle = [IO.File]::Open($path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
    try { return $handle.Length } finally { $handle.Dispose() }
}
function Get-DownloadTime {
    return [Diagnostics.Stopwatch]::GetTimestamp() / [double][Diagnostics.Stopwatch]::Frequency
}
function Write-DownloadProgress($size, $previousSize, $now, $previousTime, $started, $attempt) {
    $speed = [Math]::Max(0, $size - $previousSize) / 1024 / ($now - $previousTime)
    $waiting = if ($size -le $previousSize) { '；正在等待数据' } else { '' }
    Write-Log ('[download] 已下载 {0:F2} MiB；最近速度 {1:F1} KiB/s；本次耗时 {2:F0} 秒；尝试 {3}/3{4}' -f ($size / 1MB), $speed, ($now - $started), $attempt, $waiting)
}
function Get-CPythonArchive {
    # 调用方必须持有安装锁；可注入参数仅用于隔离测试，生产入口使用固定默认值。
    param([string]$url, [string]$sha, [string]$cache, [string]$curl,
          [int]$connectTimeout = 30, [int]$maxTime = 900,
          [int]$speedTime = 120, [int]$speedLimit = 1024,
          [double]$progressInterval = 10, [double[]]$retryDelays = @(2, 5),
          [string]$protocol = '=https')
    $part = $cache + '.part'
    Assert-DownloadFile $cache
    Assert-DownloadFile $part
    Test-Cancel
    if ([IO.File]::Exists($cache)) {
        Write-Log '[stage] 校验已有 CPython 缓存 SHA-256'
        if ((Get-ArchiveHash $cache) -ne $sha) { throw "SHA-256 校验失败，缓存已保留，请移走异常文件后重新下载: $cache" }
        return
    }
    if ([IO.File]::Exists($part)) {
        Write-Log '[stage] 校验已保留下载的 SHA-256'
        if ((Get-ArchiveHash $part) -eq $sha) {
            Test-Cancel
            [IO.File]::Move($part, $cache)
            Write-Log '[stage] 完整临时文件 SHA-256 校验通过，已发布缓存'
            return
        }
    }
    try {
        for ($attempt = 1; $attempt -le 3; $attempt++) {
            Test-Cancel
            Assert-DownloadFile $part
            $previousSize = Get-DownloadSize $part
            $mode = if ($previousSize -gt 0) { '续传' } else { '下载' }
            Write-Log ('[stage] 正在{0} CPython：已保留 {1:F2} MiB，尝试 {2}/3' -f $mode, ($previousSize / 1MB), $attempt)
            $start = New-Object Diagnostics.ProcessStartInfo
            $start.FileName = $curl
            $start.Arguments = '--disable --fail --location --silent --show-error --continue-at - --connect-timeout ' + $connectTimeout + ' --max-time ' + $maxTime + ' --speed-time ' + $speedTime + ' --speed-limit ' + $speedLimit + ' --proto "' + $protocol + '" --proto-redir "' + $protocol + '" --tlsv1.2 --write-out "%{http_code}" --output "' + $part + '" "' + $url.Replace('"', '\"') + '"'
            $start.UseShellExecute = $false
            $start.CreateNoWindow = $true
            $start.RedirectStandardError = $true
            $start.RedirectStandardOutput = $true
            $download = [Diagnostics.Process]::Start($start)
            $errorTask = $download.StandardError.ReadToEndAsync()
            $statusTask = $download.StandardOutput.ReadToEndAsync()
            $started = Get-DownloadTime
            $previousTime = $started
            try {
                while (-not $download.WaitForExit(200)) {
                    Test-Cancel
                    $now = Get-DownloadTime
                    if ($now - $previousTime -ge $progressInterval) {
                        $size = Get-DownloadSize $part
                        Write-DownloadProgress $size $previousSize $now $previousTime $started $attempt
                        $previousTime = $now
                        $previousSize = $size
                    }
                }
                $curlCode = $download.ExitCode
            } finally {
                # 取消或异常也必须等待 curl 退出并读完诊断，才允许释放安装锁。
                if (-not $download.HasExited) { $download.Kill() }
                $download.WaitForExit()
                $errorText = $errorTask.Result
                $httpStatus = $statusTask.Result.Trim()
                $download.Dispose()
                if ($errorText) { Write-Log $errorText.TrimEnd() }
            }
            Test-Cancel
            $size = Get-DownloadSize $part
            Write-Log ('[download] 本次传输结束：curl exit {0}；HTTP {1}；已保留 {2:F2} MiB' -f $curlCode, $httpStatus, ($size / 1MB))
            if ($curlCode -eq 0 -or $curlCode -eq 33 -or $httpStatus -eq '416') {
                Write-Log '[stage] 校验下载文件 SHA-256'
                if ([IO.File]::Exists($part) -and (Get-ArchiveHash $part) -eq $sha) {
                    Test-Cancel
                    [IO.File]::Move($part, $cache)
                    Write-Log '[stage] CPython 下载完成，SHA-256 校验通过，已发布缓存'
                    return
                }
                throw "SHA-256 校验失败或服务端不支持续传 (curl exit $curlCode; HTTP $httpStatus)，请移走异常文件后重新下载: $part"
            }
            $retryable = ($curlCode -in @(5, 6, 7, 18, 28, 52, 55, 56)) -or
                         ($curlCode -eq 22 -and $httpStatus -in @('408', '429', '500', '502', '503', '504'))
            if (-not $retryable -or $attempt -eq 3) { throw "curl failed (exit $curlCode; HTTP $httpStatus)" }
            $delay = $retryDelays[$attempt - 1]
            Write-Log ('[stage] 连接中断，已保留 {0:F2} MiB，{1} 秒后继续下载。' -f ($size / 1MB), $delay)
            $retryStart = Get-DownloadTime
            while ((Get-DownloadTime) - $retryStart -lt $delay) { Test-Cancel; Start-Sleep -Milliseconds 200 }
        }
    } catch {
        Write-Log ('[download] 已保留 {0:F2} MiB；已保留下载文件，重新执行安装入口可继续下载: {1}' -f ((Get-DownloadSize $part) / 1MB), $part)
        throw
    }
}
function Select-CPythonArchive([string]$packages, [string]$asset, [string]$sha,
                               [string]$cache, [string]$url, [string]$curl) {
    # 本地包优先且只读；异常包必须停止，不能以联网下载绕过。
    Test-Cancel
    $localArchive = Join-Path $packages $asset
    Assert-DownloadFile $localArchive
    if ([IO.File]::Exists($localArchive)) {
        Write-Log "[stage] 校验本地 CPython 包 SHA-256: $localArchive"
        if ((Get-ArchiveHash $localArchive) -ne $sha) { throw "本地 CPython 包 SHA-256 校验失败，文件已保留: $localArchive" }
        Test-Cancel
        Write-Log "[stage] 使用本地 CPython 包: $localArchive"
        return $localArchive
    }
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($cache)) | Out-Null
    Get-CPythonArchive -url $url -sha $sha -cache $cache -curl $curl
    Test-Cancel
    return $cache
}
function Assert-SafeArchive([string]$archive, [string]$tar) {
    Write-Log '[stage] SHA-256 verified; checking archive paths'
    $names = @(& $tar -tzf $archive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ($names -join "`n") }
    foreach ($name in $names) {
        if ($name -notmatch '^python/' -or $name -match '(^|/)\.\.(/|$)|[\\:]') { throw "Unsafe archive path: $name" }
    }
    $details = @(& $tar -tvzf $archive 2>&1)
    if ($LASTEXITCODE -ne 0) { throw ($details -join "`n") }
    foreach ($entry in $details) {
        if ($entry -notmatch '^[-d]') { throw "Unsupported archive member: $entry" }
    }
    Test-Cancel
}
# 安装主体与下载函数分隔，测试只加载上面的生产函数。
# INSTALL_MAIN_BEGIN
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
    $lock = Open-BootstrapLock $lockPath
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
            if (-not (Test-Path -LiteralPath $tar)) { throw "Required system tool missing: $tar" }
            $asset = 'cpython-3.12.12+20251014-x86_64-pc-windows-msvc-install_only.tar.gz'
            $url = 'https://github.com/astral-sh/python-build-standalone/releases/download/20251014/' + $asset.Replace('+', '%2B')
            $sha = '2d670beb3b930d30e3a13cc909923a001dbdfcb5537692d5da40b6b41643ce1c'
            $cacheDir = Join-Path $root 'downloads'
            $cache = Join-Path $cacheDir ($sha + '.tar.gz')
            $archive = Select-CPythonArchive -packages (Join-Path $env:LCC_SETUP_DIR 'cpython_packages') -asset $asset -sha $sha -cache $cache -url $url -curl $curl
            Assert-SafeArchive $archive $tar
            Test-Cancel
            [IO.Directory]::CreateDirectory($base) | Out-Null
            Write-Log "[stage] Extracting Python to final location: $base"
            & $tar -xzf $archive -C $base 2>&1 | ForEach-Object { Write-Log "$_" }
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
