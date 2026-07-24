#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python="${PYTHON:-}"
if [ -z "$python" ]; then
    for candidate in python3 python py; do
        if command -v "$candidate" >/dev/null 2>&1; then
            python="$candidate"
            break
        fi
    done
fi
[ -n "$python" ] || { echo "Python not found" >&2; exit 1; }

echo "Python: $("$python" --version)"

scratch="$root/build/__pycache__"
export PYTHONPYCACHEPREFIX="$scratch"

if [ "${SKIP_TESTS:-false}" != "true" ]; then
    "$python" -m unittest discover -s tests
fi

"$python" -m pip install --upgrade --quiet pyinstaller

"$python" -m PyInstaller --clean --noconfirm \
    --distpath dist --workpath "$scratch" build/ccas.spec

echo
echo "Artifacts in $root/dist:"
ls -la dist/
