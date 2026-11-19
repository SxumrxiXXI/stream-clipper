#!/usr/bin/env bash
# Save this run's records to the private repository, keeping whatever another
# run saved in the meantime.
#
#   deploy/save_state.sh "<commit message>"
#
# Run from a checkout of the public repository that has the private one in
# private/. STATE_BRANCH picks the branch (default main); any other branch is a
# trial's scratch copy and is simply overwritten.
#
# The daily run, the post check and the live watcher can all be saving at once.
# Logs and the .jsonl records only ever grow, so git's "union" merge joins them.
# The posting queue is merged clip by clip by deploy/merge_queue.py. Both are
# switched on by private/.gitattributes plus the settings made here.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
branch="${STATE_BRANCH:-main}"
[[ "$branch" =~ ^[A-Za-z0-9._/-]+$ ]] || { echo "bad branch name"; exit 1; }
cd "$here/../private" || { echo "no private checkout"; exit 1; }

git config user.name "${STATE_AUTHOR:-stream-clipper}"
git config user.email "actions@users.noreply.github.com"
git config merge.queue.name "posting queue, merged clip by clip"
git config merge.queue.driver "python3 '$here/merge_queue.py' %O %A %B"

git add -A
git diff --cached --quiet || git commit -q -m "${1:-Records}"

if [ "$branch" != main ]; then
  git push -q --force origin "HEAD:refs/heads/${branch}" 2> /dev/null || { echo "could not save the records"; exit 1; }
  echo "records saved to the private repository (${branch})"
  exit 0
fi

for try in 1 2 3 4; do
  if git pull -q --rebase origin main 2> /dev/null && git push -q origin HEAD:main 2> /dev/null; then
    echo "records saved to the private repository (main)"
    exit 0
  fi
  git rebase --abort 2> /dev/null || true
  sleep $((try * 7))
done
echo "could not save the records (another run kept getting in first, or the merge failed)"
exit 1
