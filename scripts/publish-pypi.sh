#!/usr/bin/env bash
# Build and publish the civicpay package to PyPI.
#
# PRIMARY PATH (recommended): publish via GitHub Actions Trusted Publishing
# (OIDC) by pushing a v* tag — see .github/workflows/install.yml. No token
# secret is needed; PyPI authenticates the workflow via its GitHub identity.
#
# THIS SCRIPT (fallback): for manual/local publishing with an API token.
#   scripts/publish-pypi.sh           # build + upload (requires TWINE_TOKEN)
#   scripts/publish-pypi.sh --check    # build only, run twine check
#
# Requires: pip install build twine
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "==> Cleaning previous build artifacts"
rm -rf dist build *.egg-info civicpay/_version.py

echo "==> Building sdist + wheel (version derived from git tags via hatch-vcs)"
python -m build

echo "==> Running twine check (metadata + description rendering)"
python -m twine check dist/*

if [[ "${1:-}" == "--check" ]]; then
  echo "==> --check: skipping upload"
  exit 0
fi

if [[ -z "${TWINE_TOKEN:-}" ]]; then
  echo "ERROR: set TWINE_TOKEN (a PyPI API token) to publish manually." >&2
  echo "       Tip: prefer Trusted Publishing — push a 'v*' tag and let the" >&2
  echo "       GitHub Actions workflow publish via OIDC (no token needed)." >&2
  exit 1
fi

echo "==> Uploading to PyPI (API token fallback)"
python -m twine upload --non-interactive -u __token__ -p "$TWINE_TOKEN" dist/*

echo "==> Published."
