#!/bin/bash

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"
DEPENDENCY_STAMP="$VENV_DIR/.mpe-pyproject.sha256"
CHECK_ONLY=0
STATUS_ONLY=0
START_ONLY=0
OPEN_ADMIN=1

fail() {
    printf '[MPE] ERROR: %s\n' "$1" >&2
    exit 1
}

python_is_compatible() {
    "$1" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
        >/dev/null 2>&1
}

dependency_digest() {
    "$1" -c \
        'import hashlib, pathlib; print(hashlib.sha256(pathlib.Path("pyproject.toml").read_bytes()).hexdigest())'
}

environment_is_ready() {
    local current_digest saved_digest
    [ -x "$VENV_PYTHON" ] || return 1
    python_is_compatible "$VENV_PYTHON" || return 1
    "$VENV_PYTHON" -c \
        'import fastapi, jsonschema, model_processing_engine, pydantic, uvicorn' \
        >/dev/null 2>&1 || return 1
    "$VENV_PYTHON" -m pip check >/dev/null 2>&1 || return 1
    [ -f "$DEPENDENCY_STAMP" ] || return 1
    current_digest="$(dependency_digest "$VENV_PYTHON")" || return 1
    saved_digest="$(tr -d '\r\n' < "$DEPENDENCY_STAMP")" || return 1
    [ "$current_digest" = "$saved_digest" ]
}

find_compatible_python() {
    local candidate
    for candidate in \
        /opt/homebrew/bin/python3.13 \
        /opt/homebrew/bin/python3.12 \
        /opt/homebrew/bin/python3.11 \
        /opt/homebrew/bin/python3.10 \
        /usr/local/bin/python3.13 \
        /usr/local/bin/python3.12 \
        /usr/local/bin/python3.11 \
        /usr/local/bin/python3.10 \
        python3.13 python3.12 python3.11 python3.10 python3 python
    do
        if command -v "$candidate" >/dev/null 2>&1 && python_is_compatible "$candidate"; then
            printf '%s\n' "$candidate"
            return 0
        fi
    done
    return 1
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --check-only) CHECK_ONLY=1; OPEN_ADMIN=0 ;;
        --status-only) STATUS_ONLY=1; OPEN_ADMIN=0 ;;
        --start-only) START_ONLY=1; OPEN_ADMIN=0 ;;
        --no-open) OPEN_ADMIN=0 ;;
        *) fail "Usage: ./start_mpe.command [--check-only | --status-only | --start-only] [--no-open]" ;;
    esac
    shift
done
[ "$((CHECK_ONLY + STATUS_ONLY + START_ONLY))" -le 1 ] || \
    fail "Use only one of --check-only, --status-only, or --start-only"
[ -f "$SCRIPT_DIR/pyproject.toml" ] || fail "pyproject.toml is missing from $SCRIPT_DIR"

cd "$SCRIPT_DIR" || fail "Cannot enter $SCRIPT_DIR"
export MPE_PROJECT_DIR="$SCRIPT_DIR"

if [ "$STATUS_ONLY" -eq 1 ]; then
    [ -x "$VENV_PYTHON" ] || fail \
        "The local environment is not ready. Run ./start_mpe.command once to repair it."
    "$VENV_PYTHON" -m model_processing_engine.cli status
    exit $?
fi

if [ "$CHECK_ONLY" -eq 1 ]; then
    environment_is_ready || fail \
        "The local environment is not ready. Run ./start_mpe.command once to repair it."
    printf '[MPE] Environment check passed: %s\n' \
        "$("$VENV_PYTHON" --version 2>&1)"
    "$VENV_PYTHON" -m model_processing_engine.cli status
    exit $?
fi

if [ -x "$VENV_PYTHON" ] && ! python_is_compatible "$VENV_PYTHON"; then
    BACKUP_DIR="$VENV_DIR.invalid-$(date '+%Y%m%d-%H%M%S')-$$"
    printf '[MPE] Existing .venv is incompatible; preserving it at %s\n' "$BACKUP_DIR"
    mv "$VENV_DIR" "$BACKUP_DIR" || fail "Could not preserve the incompatible .venv"
fi

if [ ! -x "$VENV_PYTHON" ]; then
    BASE_PYTHON="$(find_compatible_python)" || fail \
        "Python 3.10 or newer was not found. Install Python 3.12 and try again."
    printf '[MPE] Creating .venv with %s\n' "$BASE_PYTHON"
    "$BASE_PYTHON" -m venv "$VENV_DIR" || fail "Could not create .venv"
fi

python_is_compatible "$VENV_PYTHON" || fail "The .venv Python must be 3.10 or newer"
if environment_is_ready; then
    printf '[MPE] Reusing the ready .venv; pyproject.toml is unchanged\n'
elif [ ! -f "$DEPENDENCY_STAMP" ] && \
    python_is_compatible "$VENV_PYTHON" && \
    "$VENV_PYTHON" -c \
        'import fastapi, jsonschema, model_processing_engine, pydantic, uvicorn' \
        >/dev/null 2>&1 && \
    "$VENV_PYTHON" -m pip check >/dev/null 2>&1
then
    printf '[MPE] Adopting the existing ready .venv and recording its dependency state\n'
    dependency_digest "$VENV_PYTHON" > "$DEPENDENCY_STAMP" || \
        fail "Could not record the dependency state"
else
    if ! "$VENV_PYTHON" -m pip --version >/dev/null 2>&1; then
        "$VENV_PYTHON" -m ensurepip --upgrade || fail "Could not repair pip in .venv"
    fi
    printf '[MPE] Installing or refreshing dependencies from pyproject.toml\n'
    "$VENV_PYTHON" -m pip install --disable-pip-version-check -e "$SCRIPT_DIR" || \
        fail "Dependency installation failed"
    dependency_digest "$VENV_PYTHON" > "$DEPENDENCY_STAMP" || \
        fail "Could not record the dependency state"
    environment_is_ready || fail "The repaired environment did not pass validation"
fi

printf '[MPE] Starting the managed service\n'
"$VENV_PYTHON" -m model_processing_engine.cli start || exit $?
if [ "$START_ONLY" -eq 1 ]; then
    exit 0
fi
"$VENV_PYTHON" -m model_processing_engine.cli status
if [ "$OPEN_ADMIN" -eq 1 ]; then
    printf '[MPE] Opening the local management page\n'
    "$VENV_PYTHON" -m model_processing_engine.cli admin || true
fi
