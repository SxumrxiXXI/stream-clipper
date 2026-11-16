#!/bin/zsh
# Copy this Mac's settings and records into the private repository and push
# them. Run it before handing the daily run over to GitHub, so GitHub starts
# from the Mac's queue. The private clone lives in private/ (ignored here).
set -euo pipefail
cd "${0:A:h}/.."
login=$(gh api user --jq .login)
[ -d private/.git ] || gh repo clone "$login/stream-clipper-private" private -- -q
git -C private pull -q --ff-only 2>/dev/null || true
mkdir -p private/data
rsync -a --delete config/ private/config/
rsync -a --delete --exclude chat --exclude windows --exclude compare --exclude progress.json \
  --exclude daily.lock --exclude 'scan/candidates' data/ private/data/
cp CLAUDE.md private/CLAUDE.md
cat > private/.gitignore <<'IGN'
# Working files that are rebuilt on every run
data/chat/
data/windows/
data/compare/
data/scan/candidates/
data/progress.json
data/daily.lock
.DS_Store
IGN
git -C private add -A
if git -C private diff --cached --quiet; then echo "private repository already up to date"; exit 0; fi
git -C private -c user.name="$login" -c user.email="$(gh api user --jq .id)+$login@users.noreply.github.com" \
  commit -q -m "Settings and records from the Mac, $(date '+%F %H:%M')"
git -C private push -q origin HEAD:main
echo "pushed settings and records to $login/stream-clipper-private"
