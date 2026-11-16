#!/bin/zsh
# One-time setup, run by the owner on their own machine. It
#   1. copies the four API keys from .env into the public repository's
#      encrypted secrets (GitHub encrypts them; they are never shown again), and
#   2. creates a key that lets the daily workflow read and write the private
#      repository, and nothing else: the public half is registered on the
#      private repository, the private half becomes one more secret, and the
#      local copy is deleted.
set -euo pipefail
cd "${0:A:h}/.."
login=$(gh api user --jq .login)
pub="$login/stream-clipper"; priv="$login/stream-clipper-private"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT

grep -E '^(GEMINI_API_KEY|UPLOAD_POST_API_KEY|TWITCH_CLIENT_ID|TWITCH_CLIENT_SECRET)=.' .env > "$tmp/keys.env"
[ "$(wc -l < "$tmp/keys.env" | tr -d ' ')" = 4 ] || { echo "one of the four keys is missing or empty in .env"; exit 1; }
gh secret set -f "$tmp/keys.env" -R "$pub"

ssh-keygen -q -t ed25519 -N "" -C "stream-clipper daily workflow" -f "$tmp/key"
gh repo deploy-key add "$tmp/key.pub" -R "$priv" --allow-write --title "daily workflow"
gh secret set STATE_DEPLOY_KEY -R "$pub" < "$tmp/key"

echo
echo "Secrets now set on $pub:"
gh secret list -R "$pub"
