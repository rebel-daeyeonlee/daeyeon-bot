#!/usr/bin/env bash
# Register daeyeon-bot as a systemd user unit (no root required).
#
# Usage:  ./scripts/install-linux.sh /path/to/oauth_token
# The credential file must be 0600. systemd copies it into the unit's
# CREDENTIALS_DIRECTORY at start time so the file path is reproducible
# across machines.
set -euo pipefail

if [[ "$(uname)" != "Linux" ]]; then
  echo "install-linux.sh: requires Linux (got $(uname))" >&2
  exit 1
fi

if [[ $# -lt 1 ]]; then
  echo "usage: $0 /path/to/oauth_token" >&2
  exit 64  # EX_USAGE
fi

CREDENTIAL_PATH="$1"
if [[ ! -f "$CREDENTIAL_PATH" ]]; then
  echo "credential file not found: $CREDENTIAL_PATH" >&2
  exit 66  # EX_NOINPUT
fi

PREFIX="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${DAEYEON_BOT_STATE_DIR:-$HOME/.daeyeon-bot}"
UNIT_DIR="$HOME/.config/systemd/user"
UNIT_NAME="daeyeon-bot.service"
TARGET="$UNIT_DIR/$UNIT_NAME"
TEMPLATE="$PREFIX/deploy/systemd/$UNIT_NAME"

mkdir -p "$STATE_DIR" "$UNIT_DIR"
chmod 0700 "$STATE_DIR"

# Refuse to copy a world / group-readable credential — the daemon would
# refuse to read it anyway via FileSecrets perms enforcement.
mode="$(stat -c '%a' "$CREDENTIAL_PATH")"
if [[ "$mode" != "600" ]]; then
  echo "credential at $CREDENTIAL_PATH has perms $mode; expected 600" >&2
  exit 78  # EX_CONFIG
fi

sed \
  -e "s|__INSTALL_PREFIX__|$PREFIX|g" \
  -e "s|__STATE_DIR__|$STATE_DIR|g" \
  -e "s|__CREDENTIAL_PATH__|$CREDENTIAL_PATH|g" \
  "$TEMPLATE" > "$TARGET"

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT_NAME"

echo "installed: $TARGET"
echo "status:    systemctl --user status $UNIT_NAME"
echo "logs:      journalctl --user -u $UNIT_NAME -f"
echo "stop:      systemctl --user stop $UNIT_NAME"
