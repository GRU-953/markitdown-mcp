#!/usr/bin/env bash
# Create the local Python virtual environment for the MarkItDown Attachments
# MCP server and install its dependencies. Safe to re-run (idempotent-ish).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# markitdown officially supports Python 3.10–3.13. Prefer 3.12.
PY=""
for c in python3.12 python3.11 python3.13 python3.10; do
  if command -v "$c" >/dev/null 2>&1; then PY="$(command -v "$c")"; break; fi
done
if [ -z "$PY" ] && command -v python3 >/dev/null 2>&1; then
  v="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$v" in 3.10|3.11|3.12|3.13) PY="$(command -v python3)" ;; esac
fi
if [ -z "$PY" ]; then
  echo "ERROR: No supported Python (3.10–3.13) found." >&2
  echo "Install one, e.g.:  brew install python@3.12   then re-run ./install.sh" >&2
  exit 1
fi

echo "Using $PY ($($PY --version 2>&1))"
"$PY" -m venv "$HERE/.venv"
"$HERE/.venv/bin/python" -m pip install --upgrade pip wheel >/dev/null
"$HERE/.venv/bin/python" -m pip install -r "$HERE/requirements.txt"

echo
echo "✓ Installed. Virtualenv: $HERE/.venv"
echo "  Server entry:        $HERE/server/markitdown_attachments_server.py"
echo "  Smoke test:          $HERE/.venv/bin/python $HERE/server/markitdown_attachments_server.py --selftest"
