#!/usr/bin/env bash
# 创建插件统一独立运行时，按 QGIS、系统发行版、独立 Python 的顺序探测。
set -euo pipefail

sort_versions() {
    # macOS 的 sort 不支持 -V，使用补齐的数字段作为排序键。
    awk -F '\t' '{
        n=split($1, v, ".");
        printf "%08d.%08d.%08d.%08d\t%s\n", v[1], v[2], v[3], v[4], $2
    }' | LC_ALL=C sort -r | cut -f2-
}

qgis_app_candidates() {
    local app version python
    local roots=("/Applications" "${HOME}/Applications")
    for app in "${roots[@]}"; do
        for app in "$app"/QGIS*.app; do
            [[ -d "$app" ]] || continue
            version=""
            if [[ -x /usr/libexec/PlistBuddy ]]; then
                version="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$app/Contents/Info.plist" 2>/dev/null || true)"
            fi
            if [[ -z "$version" ]]; then
                version="$(basename "$app" | sed -E 's/^[^0-9]*//; s/[^0-9.].*$//')"
            fi
            printf '%s\t%s\n' "${version:-0}" "$app"
        done
    done | sort_versions | while IFS= read -r app; do
        app_python_candidates "$app"
    done
}

app_python_candidates() {
    local app="$1" python version
    for python in "$app"/Contents/Frameworks/Python.framework/Versions/*/bin/python3; do
        [[ -x "$python" ]] || continue
        version="${python%/bin/python3}"
        printf '%s\t%s\n' "${version##*/}" "$python"
    done | sort_versions
    printf '%s\n' "$app/Contents/MacOS/bin/python3"
}

prefix_candidates() {
    local prefix="$1"
    # 兼容 /usr、/usr/share/qgis 和 app 内安装前缀。
    printf '%s\n' "$prefix/bin/python3" "$prefix/../bin/python3" "$prefix/../../bin/python3"
}

qgis_candidates() {
    local command_path prefix
    if [[ -n "${ORIGINAL_QGIS_PREFIX:-}" ]]; then
        prefix_candidates "$ORIGINAL_QGIS_PREFIX"
        if [[ "$RUNTIME_PLATFORM" == Darwin && "$ORIGINAL_QGIS_PREFIX" == *.app/* ]]; then
            app_python_candidates "${ORIGINAL_QGIS_PREFIX%%.app*}.app"
        fi
    fi
    if [[ "${RUNTIME_PLATFORM}" == Darwin ]]; then
        qgis_app_candidates
    else
        for command_path in qgis qgis-ltr; do
            command_path="$(command -v "$command_path" || true)"
            [[ -n "$command_path" ]] || continue
            prefix="$(cd "$(dirname "$command_path")/.." && pwd -P)"
            prefix_candidates "$prefix"
        done
        printf '%s\n' /usr/bin/python3 /usr/local/bin/python3
    fi
}

try_candidate() {
    local candidate="$1" source="$2" item
    if [[ "$candidate" != */* ]]; then candidate="$(command -v "$candidate" || true)"; fi
    if [[ -z "$candidate" || ! -x "$candidate" ]]; then
        printf 'Unavailable candidate: %s\n' "$1" >&2
        return 1
    fi
    candidate="$(cd "$(dirname "$candidate")" && pwd -P)/$(basename "$candidate")"
    for item in "${SEEN[@]+"${SEEN[@]}"}"; do
        [[ "$item" != "$candidate" ]] || return 1
    done
    SEEN+=("$candidate")
    printf 'Checking %s: %s\n' "$source" "$candidate" >&2
    if "$candidate" "$HELPER" --probe --target "$VENV_DIR" --link "$VENV_LINK"; then
        SELECTED_PYTHON="$candidate"
        SELECTED_SOURCE="$source"
        return 0
    fi
    printf 'Candidate failed. If venv/ensurepip is missing, install your distribution python3-venv package.\n' >&2
    return 1
}

select_python() {
    local candidate
    if [[ -n "${SAM_PYTHON:-}" ]]; then
        if try_candidate "$SAM_PYTHON" SAM_PYTHON; then return 0; fi
        echo 'Invalid SAM_PYTHON; no automatic fallback is allowed.' >&2
        return 1
    fi
    while IFS= read -r candidate; do
        if try_candidate "$candidate" 'QGIS/system distribution Python'; then return 0; fi
    done < <(qgis_candidates)
    echo 'No usable QGIS Python. Checking standalone Python.' >&2
    while IFS= read -r candidate; do
        if try_candidate "$candidate" 'PATH Python'; then return 0; fi
    done < <(type -a -p python3 python 2>/dev/null || true)
    echo 'No usable Python. Flatpak/Snap/AppImage Python may be inaccessible; set SAM_PYTHON to an external Python with venv/ensurepip.' >&2
    return 1
}

main() {
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
    VENV_LINK="${SCRIPT_DIR}/venv"
    VENV_DIR="${SAM_VENV_DIR:-$VENV_LINK}"
    HELPER="${SCRIPT_DIR}/runtime_setup.py"
    if [[ "${1:-}" != --discover-only && "${SAM_RECREATE:-0}" != 1 ]] &&
        [[ -e "$VENV_LINK" || -L "$VENV_LINK" || -e "$VENV_DIR" || -L "$VENV_DIR" ]]; then
        echo 'Existing runtime found. Set SAM_RECREATE=1 to rebuild.' >&2
        return 1
    fi
    ORIGINAL_QGIS_PREFIX="${QGIS_PREFIX_PATH:-}"
    unset PYTHONHOME PYTHONPATH PYTHONUSERBASE QGIS_PREFIX_PATH VIRTUAL_ENV
    export PYTHONNOUSERSITE=1 PYTHONUTF8=1 PYTHONIOENCODING=utf-8:backslashreplace
    RUNTIME_PLATFORM="$(uname -s)"
    SEEN=()
    select_python || return 1
    printf 'Selected %s: %s\n' "$SELECTED_SOURCE" "$SELECTED_PYTHON"
    [[ "${1:-}" != --discover-only ]] || return 0
    "$SELECTED_PYTHON" "$HELPER" --target "$VENV_DIR" --link "$VENV_LINK" --source "$SELECTED_SOURCE"
}

# 允许回归测试加载发现函数，不执行创建流程。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
