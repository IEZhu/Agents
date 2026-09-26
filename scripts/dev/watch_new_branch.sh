#!/bin/bash
# Wait for a new remote branch under a prefix, e.g. the results branch a cloud run pushes.
#
#   scripts/dev/watch_new_branch.sh claude/ablation-          # checks once a minute for an hour
#   WAIT_MINUTES=180 scripts/dev/watch_new_branch.sh claude/ablation-
#
# The branches present at the first check are the baseline; the script prints and
# exits as soon as one appears that was not there.
set -u
prefix=${1:?usage: $0 BRANCH-PREFIX}
list() { git ls-remote origin "refs/heads/${prefix}*" | awk '{print $2}' | sed 's#refs/heads/##' | sort; }
known=$(list)
for _ in $(seq 1 "${WAIT_MINUTES:-60}"); do
  sleep 60
  new=$(comm -13 <(echo "$known") <(list))
  if [ -n "$new" ]; then echo "NEW: $new"; exit 0; fi
done
echo "TIMEOUT: no new ${prefix}* branch"
exit 1
