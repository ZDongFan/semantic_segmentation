#!/usr/bin/env bash
# Linux/macOS 只下载固定独立 Python，所有依赖安装复用公共实现。
set -euo pipefail

select_platform() {
    local system="$1" machine="$2" arm_capable="${3:-0}"
    if [[ "$system" == Darwin && "$arm_capable" == 1 ]]; then machine=arm64; fi
    case "$system/$machine" in
        Linux/x86_64)
            ASSET=cpython-3.12.12%2B20251014-x86_64-unknown-linux-gnu-install_only.tar.gz
            SHA=1ab2b6594d1c3d76cbebea09d6bc3e6ba68d8eb3b6322080375c4cc3dd188f34 ;;
        Darwin/x86_64)
            ASSET=cpython-3.12.12%2B20251014-x86_64-apple-darwin-install_only.tar.gz
            SHA=9b8589eefb153cbe7cb652993d0ecc94aeb2fa13c1a2e8bc240f5f74f23bb21b ;;
        Darwin/arm64|Darwin/aarch64)
            ASSET=cpython-3.12.12%2B20251014-aarch64-apple-darwin-install_only.tar.gz
            SHA=6ceba34fe78802853a30bde6f303a0a54f71f6ab07a673da34e90c0aa06c786e ;;
        */i386|*/i486|*/i586|*/i686|*/armv7l)
            echo '32 位系统不受当前 PyTorch/SAM2 依赖链支持。' >&2; return 1 ;;
        *) echo "当前安装脚本未提供该平台组合: $system / $machine" >&2; return 1 ;;
    esac
    PLATFORM="$system/$machine"
}

check_cancel() {
    if [[ -n "${LCC_CANCEL_FILE:-}" && -e "$LCC_CANCEL_FILE" ]]; then
        echo 'Installation cancelled by user; incomplete directories retained.' >&2
        return 130
    fi
}

hash_file() {
    if command -v sha256sum >/dev/null; then sha256sum "$1" | awk '{print $1}'
    else shasum -a 256 "$1" | awk '{print $1}'; fi
}

safe_archive() {
    # 校验固定归档内所有路径和链接；不允许绝对路径、父级路径、设备和硬链接。
    local archive="$1" names="$2" details="$3"
    tar -tzf "$archive" > "$names"
    tar -tvzf "$archive" > "$details"
    awk '$0 !~ /^python\// || $0 ~ /(^|\/)\.\.(\/|$)|\\|:/ {bad=1} END {exit bad}' "$names" || {
        echo 'Unsafe archive member path' >&2; return 1;
    }
    awk '
        substr($0,1,1) !~ /[-dl]/ {bad=1}
        substr($0,1,1)=="l" {
            n=split($0,a," -> "); target=a[n];
            if(n!=2 || target ~ /^\/|(^|\/)\.\.(\/|$)|\\|:/) bad=1
        }
        END {exit bad}
    ' "$details" || { echo 'Unsafe archive member or link' >&2; return 1; }
}

main() {
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
    local system machine arm_capable=0 tool part cache base python target link
    lock=''
    pid=''
    mkdir -p "$SCRIPT_DIR/logs"
    export LCC_LOG_PATH="$SCRIPT_DIR/logs/install-$(date +%Y%m%d-%H%M%S)-$$.log"
    # 终端与日志使用同一脱敏流；公共 Python 不再重复写入文件。
    exec > >(awk '{gsub(/(https?|socks5h?|socks4):\/\/[^[:space:]<>]+/, "[redacted URL]"); print; fflush()}' | tee -a "$LCC_LOG_PATH") 2>&1
    export LCC_LOG_CAPTURED=1
    echo "Log: $LCC_LOG_PATH"
    system="$(uname -s)"
    machine="$(uname -m)"
    if [[ "$system" == Darwin ]]; then
        arm_capable="$(/usr/sbin/sysctl -n hw.optional.arm64 2>/dev/null || true)"
    fi
    select_platform "$system" "$machine" "$arm_capable"
    echo "[stage] Platform: $PLATFORM"
    [[ -z "${SAM_PYTHON:-}" ]] || echo 'SAM_PYTHON is no longer used; standalone CPython 3.12.12 is downloaded.'
    [[ -z "${SAM_RECREATE:-}" ]] || echo 'SAM_RECREATE is no longer supported; existing environments are checked only.'
    for tool in curl tar awk tee; do command -v "$tool" >/dev/null || { echo "Required tool missing: $tool"; return 1; }; done
    command -v sha256sum >/dev/null || command -v shasum >/dev/null || { echo 'Required SHA-256 tool missing'; return 1; }
    link="$SCRIPT_DIR/venv"
    target="${SAM_VENV_DIR:-$link}"
    [[ "$target" == /* ]] || target="$PWD/$target"
    # 下载前确认自定义目标的最近现有父目录可写，不创建或修改旧环境。
    local target_parent
    target_parent="$(dirname "$target")"
    while [[ ! -e "$target_parent" ]]; do target_parent="$(dirname "$target_parent")"; done
    [[ -d "$target_parent" && -w "$target_parent" ]] || { echo "Runtime parent is not writable: $target_parent"; return 1; }
    base="$SCRIPT_DIR/python-3.12.12-20251014"
    python="$base/python/bin/python3"
    lock="$SCRIPT_DIR/.bootstrap-lock"
    mkdir "$lock" || { echo "Another installation or interrupted lock exists: $lock"; return 1; }
    # 只释放本次持有的锁，下载中断保留临时文件和已验证缓存。
    trap 'code=$?; trap - EXIT; [[ -z "$pid" ]] || kill "$pid" 2>/dev/null || true; rm -f "$lock/names" "$lock/details"; rmdir "$lock"; echo "Exit: $code; Log: $LCC_LOG_PATH"; exit "$code"' EXIT
    trap 'exit 130' INT TERM
    # 引导阶段先隔离 QGIS 的 Python、GDAL/PROJ 与动态库注入。
    local key item cleaned_path='' qgis_prefix="${QGIS_PREFIX_PATH:-}" osgeo_root="${OSGEO4W_ROOT:-}"
    while IFS= read -r key; do
        case "$key" in
            PYTHON*|GDAL_*|PROJ_*|DYLD_*|QGIS_PREFIX_PATH|OSGEO4W_ROOT|VIRTUAL_ENV|GEOTIFF_CSV|LD_LIBRARY_PATH|LD_PRELOAD|QT_PLUGIN_PATH|QT_QPA_PLATFORM_PLUGIN_PATH) unset "$key" ;;
        esac
    done < <(compgen -e)
    local old_ifs="$IFS"
    IFS=:
    for item in $PATH; do
        [[ -n "$item" ]] || continue
        case "$item" in *[Qq][Gg][Ii][Ss]*|*[Oo][Ss][Gg][Ee][Oo]4[Ww]*) continue ;; esac
        [[ -z "$qgis_prefix" || "$item" != "$qgis_prefix"* ]] || continue
        [[ -z "$osgeo_root" || "$item" != "$osgeo_root"* ]] || continue
        cleaned_path="${cleaned_path:+$cleaned_path:}$item"
    done
    IFS="$old_ifs"
    export PATH="$cleaned_path" PYTHONNOUSERSITE=1 PYTHONUTF8=1 PYTHONUNBUFFERED=1 PYTHONIOENCODING=utf-8:backslashreplace
    check_cancel
    if [[ -e "$target" || -L "$target" ]]; then
        [[ -x "$target/bin/python" ]] || { echo "Incomplete venv retained: $target"; return 1; }
        "$target/bin/python" "$SCRIPT_DIR/runtime_setup.py" --target "$target" --link "$link" --verify-only
        return
    fi
    if [[ -e "$link" || -L "$link" ]]; then echo "Fixed entry already exists: $link"; return 1; fi
    if [[ ! -e "$base" && ! -L "$base" ]]; then
        mkdir -p "$SCRIPT_DIR/downloads"
        cache="$SCRIPT_DIR/downloads/$SHA.tar.gz"
        if [[ ! -e "$cache" ]]; then
            part="$cache.part-$$"
            echo '[stage] Downloading standalone CPython 3.12.12 / 20251014'
            curl -fL --silent --show-error --retry 2 --connect-timeout 30 --max-time 900 --proto '=https' --tlsv1.2 \
                "https://github.com/astral-sh/python-build-standalone/releases/download/20251014/$ASSET" -o "$part" &
            pid=$!
            while kill -0 "$pid" 2>/dev/null; do check_cancel; sleep 0.2; done
            wait "$pid"
            pid=''
            [[ "$(hash_file "$part")" == "$SHA" ]] || { echo "SHA-256 mismatch: $part"; return 1; }
            mv "$part" "$cache"
        fi
        [[ "$(hash_file "$cache")" == "$SHA" ]] || { echo "SHA-256 mismatch: $cache"; return 1; }
        echo '[stage] SHA-256 verified; checking archive paths'
        safe_archive "$cache" "$lock/names" "$lock/details"
        rm "$lock/names" "$lock/details"
        check_cancel
        mkdir "$base"
        echo "[stage] Extracting Python to final location: $base"
        tar -xzf "$cache" -C "$base"
        printf '%s' "$SHA" > "$base/.verified-archive"
    fi
    [[ -f "$base/.verified-archive" && "$(cat "$base/.verified-archive")" == "$SHA" ]] || {
        echo "Incomplete/unrecognized Python retained: $base"; return 1;
    }
    echo "[stage] Starting standalone Python: $python"
    "$python" "$SCRIPT_DIR/runtime_setup.py" --target "$target" --link "$link"
}

# 平台选择与归档检查可被测试加载，不运行实际安装。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
