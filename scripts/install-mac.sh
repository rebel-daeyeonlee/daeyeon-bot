#!/usr/bin/env bash
# Register daeyeon-bot as a launchd user agent.
#
# Usage:  ./scripts/install-mac.sh
# Re-run safely; the script `unload`s any previous version before loading
# the new one. The OAuth token is NOT touched here — run
# `./scripts/setup-token.sh` first.
set -euo pipefail

PREFIX="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="${DAEYEON_BOT_STATE_DIR:-$HOME/.daeyeon-bot}"
LABEL="ai.rebellions.daeyeon-bot"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
TARGET="$LAUNCH_AGENTS_DIR/$LABEL.plist"
TEMPLATE="$PREFIX/deploy/launchd/$LABEL.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "install-mac.sh: requires macOS (got $(uname))" >&2
  exit 1
fi

mkdir -p "$STATE_DIR" "$LAUNCH_AGENTS_DIR"
chmod 0700 "$STATE_DIR"

# Fill template placeholders with absolute paths.
sed \
  -e "s|__INSTALL_PREFIX__|$PREFIX|g" \
  -e "s|__STATE_DIR__|$STATE_DIR|g" \
  -e "s|__HOME__|$HOME|g" \
  "$TEMPLATE" > "$TARGET"

# Reload (unload may fail if the agent isn't registered yet — that's fine).
launchctl unload "$TARGET" 2>/dev/null || true
launchctl load -w "$TARGET"

echo "installed: $TARGET"
echo "logs:      $STATE_DIR/launchd.{out,err}.log"
echo "doctor:    just doctor"
echo "stop:      launchctl unload $TARGET"
