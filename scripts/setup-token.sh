#!/usr/bin/env bash
# Helper: store the Claude Code OAuth token in the macOS Keychain.
#
# Run `claude setup-token` first to obtain a fresh token, then paste it
# when this script prompts. Requires macOS (`security` CLI).
set -euo pipefail

if [[ "$(uname)" != "Darwin" ]]; then
  echo "setup-token.sh: macOS only — on Linux, drop the token into a 0600 file and pass it to install-linux.sh." >&2
  exit 1
fi

SERVICE="${1:-daeyeon-bot}"
ACCOUNT="${2:-oauth_token}"

echo "Storing OAuth token in Keychain (service=$SERVICE, account=$ACCOUNT)."
echo "Tip: run \`claude setup-token\` in another terminal to mint a token."
read -rsp "OAuth token: " TOKEN
echo

if [[ -z "$TOKEN" ]]; then
  echo "no token given; aborted." >&2
  exit 1
fi

# Replace existing entry if present.
security delete-generic-password -s "$SERVICE" -a "$ACCOUNT" 2>/dev/null || true
security add-generic-password -s "$SERVICE" -a "$ACCOUNT" -w "$TOKEN"

echo "stored. Verify with:  security find-generic-password -s $SERVICE -a $ACCOUNT"
