#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ ! -f "$ROOT/app/launch.py" ]]; then
    echo "Missing app/. Move start.sh together with the app folder." >&2
    exit 1
fi

for candidate in "${PYTHON:-python3.12}" python3.13 python3; do
    if command -v "$candidate" >/dev/null && "$candidate" -c \
        'import sys; sys.exit(not ((3, 12) <= sys.version_info < (3, 14)))'; then
        exec "$candidate" "$ROOT/app/launch.py" "$@"
    fi
done
echo "Python 3.12 or 3.13 is required (not 3.14). Install it or set PYTHON=/path/to/python." >&2
exit 1
