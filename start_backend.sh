#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_PYTHON="${LCC_BACKEND_PYTHON:-$SCRIPT_DIR/.venv-backend/bin/python}"
if [[ ! -x "$BACKEND_PYTHON" ]]; then
    echo "Backend Python not found: $BACKEND_PYTHON" >&2
    printf 'Prepare the backend environment with:\n' >&2
    printf '  python3.12 -m venv "%s/.venv-backend"\n' "$SCRIPT_DIR" >&2
    printf '  "%s/.venv-backend/bin/python" -m pip install -r "%s/backend/requirements.txt"\n' "$SCRIPT_DIR" "$SCRIPT_DIR" >&2
    printf 'Configure "%s/.env" using "%s/.env.example" as a template.\n' "$SCRIPT_DIR" "$SCRIPT_DIR" >&2
    echo "Or set LCC_BACKEND_PYTHON to an existing backend Python executable." >&2
    exit 1
fi
exec "$BACKEND_PYTHON" -X utf8 "$SCRIPT_DIR/backend/start.py" "$@"
