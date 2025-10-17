#!/usr/bin/env bash
set -euo pipefail

# Minimal runner for the AWS uploader on the Raspberry Pi.
# - Optionally reads SERIAL_PORT from rasp_pi_files/rasp_pi_context.txt
# - Optionally sources rasp_pi_files/env.sh for environment variables

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load optional environment (e.g., export DATABASE_URL=...)
[ -f "$SCRIPT_DIR/env.sh" ] && . "$SCRIPT_DIR/env.sh"

# Prefer SERIAL_PORT from rasp_pi_context.txt when present
if [ -z "${SERIAL_PORT:-}" ] && [ -f "$SCRIPT_DIR/rasp_pi_context.txt" ]; then
  SERIAL_PORT="$(grep -E '^\s*/' "$SCRIPT_DIR/rasp_pi_context.txt" | head -n1 | tr -d '[:space:]' || true)"
  export SERIAL_PORT
fi

# Ensure a local venv and required deps
if [ ! -f "$ROOT_DIR/venv/bin/activate" ]; then
  python3 -m venv "$ROOT_DIR/venv"
fi
. "$ROOT_DIR/venv/bin/activate"
python - <<'PY' || pip install --upgrade pip setuptools wheel && pip install pyserial 'psycopg[binary]>=3.1'
try:
    import serial, psycopg  # noqa: F401
    import sys
    sys.exit(0)
except Exception:
    import sys
    sys.exit(1)
PY

cd "$ROOT_DIR"
exec python3 "$ROOT_DIR/log_to_aws.py"


