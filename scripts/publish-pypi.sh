#!/usr/bin/env bash
# Build and publish the civicpay-open-framework package to PyPI.
#
# Usage:
#   scripts/publish-pypi.sh           # build + upload (requires TWINE_TOKEN)
#   scripts/publish-pypi.sh --check   # build only, run twine check
#
# Requires: pip install build twine
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Cleaning previous build artifacts"
rm -rf dist build *.egg-info

echo "==> Building sdist + wheel"
python -m build

echo "==> Running twine check (metadata + description rendering)"
python -m twine check dist/*

if [[ "${1:-}" == "--check" ]]; then
  echo "==> --check: skipping upload"
  exit 0
fi

if [[ -z "${TWINE_TOKEN:-}" ]]; then
  echo "ERROR: set TWINE_TOKEN (a PyPI API token) to publish." >&2
  exit 1
fi

echo "==> Uploading to PyPI"
python -m twine upload --non-interactive -u __token__ -p "$TWINE_TOKEN" dist/*

echo "==> Published."
