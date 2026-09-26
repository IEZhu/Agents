#!/bin/bash
# Wait until Copilot has reviewed the current head of each listed PR.
#
#   scripts/dev/wait_copilot.sh 90 91 93        # checks once a minute, gives up after an hour
#   WAIT_MINUTES=120 scripts/dev/wait_copilot.sh 95
#
# A PR counts as reviewed when its head on GitHub equals the branch tip on the
# remote (GitHub lags a push by up to a minute) and a review by
# copilot-pull-request-reviewer[bot] exists on that commit. Reviews are read with
# --paginate: a PR with many review rounds has more than one page.
# Request the review first:
#   gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
set -u
[ $# -gt 0 ] || { echo "usage: $0 PR [PR ...]" >&2; exit 2; }
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
root=$(git rev-parse --show-toplevel)
for _ in $(seq 1 "${WAIT_MINUTES:-60}"); do
  pending=""
  for pr in "$@"; do
    read -r branch head < <(gh pr view "$pr" --repo "$repo" --json headRefName,headRefOid --jq '"\(.headRefName) \(.headRefOid)"')
    tip=$(git -C "$root" ls-remote origin "refs/heads/$branch" | cut -f1)
    if [ "$head" != "$tip" ]; then pending="$pending $pr(head-lag)"; continue; fi
    got=$(gh api --paginate "repos/$repo/pulls/$pr/reviews" \
      --jq ".[] | select(.user.login==\"copilot-pull-request-reviewer[bot]\" and .commit_id==\"$head\") | .id" | wc -l | tr -d ' ')
    [ "$got" -ge 1 ] || pending="$pending $pr"
  done
  if [ -z "$pending" ]; then echo "COPILOT REVIEWED THE TIP OF: $*"; exit 0; fi
  sleep 60
done
echo "TIMEOUT: still waiting on$pending"
exit 1
