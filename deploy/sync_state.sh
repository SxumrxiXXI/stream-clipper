#!/bin/zsh
# Bring this machine's copy of the private settings and records up to date with
# GitHub, and send back anything changed here (a settings edit, a skipped clip).
# config/ and data/ in this folder are links into private/, so after this the
# local commands (post.py list, report, skip ...) see what the daily run saved.
set -euo pipefail
cd "${0:A:h}/.."
login=$(gh api user --jq .login)
[ -d private/.git ] || gh repo clone "$login/stream-clipper-private" private -- -q
cp CLAUDE.md private/CLAUDE.md          # the project brief is backed up with the records
git -C private add -A
if ! git -C private diff --cached --quiet; then
  git -C private -c user.name="$login" -c user.email="$(gh api user --jq .id)+$login@users.noreply.github.com" \
    commit -q -m "Changes from the Mac, $(date '+%F %H:%M')"
  echo "saved local changes"
fi
git -C private pull -q --rebase origin main
git -C private push -q origin HEAD:main
echo "in step with $login/stream-clipper-private"
