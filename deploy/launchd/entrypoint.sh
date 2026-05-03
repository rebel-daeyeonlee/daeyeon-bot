#!/usr/bin/env bash
# launchd entrypoint. Sets a tight umask so any file the daemon creates is
# private, then exec's `daeyeon-bot run` so launchd watches the right pid.
# The OAuth token is loaded from the macOS Keychain inside the process —
# never via this script's environment.
set -euo pipefail

umask 0077

cd "$(dirname "$0")/../.."

# Use `uv run` so the project venv stays the source of truth.
exec uv run daeyeon-bot run "$@"
