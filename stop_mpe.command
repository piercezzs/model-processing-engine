#!/bin/bash

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_PYTHON="$SCRIPT_DIR/.venv/bin/python"

fail() {
    printf '[MPE] ERROR: %s\n' "$1" >&2
    exit 1
}

cd "$SCRIPT_DIR" || fail "Cannot enter $SCRIPT_DIR"
[ -x "$VENV_PYTHON" ] || fail "The MPE .venv is missing. Run start_mpe.command first."
"$VENV_PYTHON" -c 'import model_processing_engine' >/dev/null 2>&1 || \
    fail "MPE is not installed in .venv. Run start_mpe.command first."

printf '[MPE] Stopping the verified managed service\n'
"$VENV_PYTHON" -m model_processing_engine.cli stop || exit $?
"$VENV_PYTHON" -m model_processing_engine.cli status
