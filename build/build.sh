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

# The version baked into the binary. CCAS_VERSION wins; otherwise a tag build
# in CI takes the tag the toolkit already normalised for us (REF_NAME_NORM,
# e.g. "v1.2.0"), so the release and `ccas --version` cannot disagree.
version="${CCAS_VERSION:-}"
if [ -z "$version" ] && [ "${REF_TYPE:-}" = tag ] && [ -n "${REF_NAME_NORM:-}" ]; then
    version="$REF_NAME_NORM"
fi
version="${version##*/}"   # "pkg/v1.2.0" -> "v1.2.0"
version="${version#[vV]}"  # "v1.2.0"     -> "1.2.0"

version_file="$root/core/version.py"
if [ -n "$version" ]; then
    cp "$version_file" "$version_file.orig"
    trap 'mv -f "$version_file.orig" "$version_file"' EXIT
    sed -i.bak "s/^__version__ = \".*\"$/__version__ = \"$version\"/" "$version_file"
    rm -f "$version_file.bak"
    echo "Version: $version (stamped into core/version.py for this build)"
fi

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
