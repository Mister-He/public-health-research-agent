#!/usr/bin/env bash
# Update the CLAUDE_CODE_OAUTH_TOKEN repository secret and immediately verify it on GitHub.
#
#   1. run `claude setup-token` and copy the token it prints
#   2. run this script and paste the token when asked (input is hidden)
#
# The token is piped straight to `gh secret set` (no file, no shell history, no stray newline),
# then the "Claude auth check" workflow is started and followed until it finishes.
set -euo pipefail

REPO="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"
WORKFLOW="claude-auth-check.yml"

printf 'Paste the token from `claude setup-token` for %s (hidden), then press Enter: ' "$REPO"
IFS= read -rs TOKEN
echo
TOKEN="$(printf %s "$TOKEN" | tr -d '[:space:]')"
if [ -z "$TOKEN" ]; then
  echo "No token entered; nothing changed." >&2
  exit 1
fi

printf %s "$TOKEN" | gh secret set CLAUDE_CODE_OAUTH_TOKEN --repo "$REPO"
unset TOKEN
echo "Secret CLAUDE_CODE_OAUTH_TOKEN updated. Starting the auth check…"

gh workflow run "$WORKFLOW" --repo "$REPO" --ref main
sleep 5   # let GitHub register the new run
RUN_ID="$(gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 1 --json databaseId -q '.[0].databaseId')"
gh run watch "$RUN_ID" --repo "$REPO" --exit-status && echo "✅ Claude OAuth token works." \
  || { echo "❌ Auth check failed — see: gh run view $RUN_ID --repo $REPO --log-failed" >&2; exit 1; }
