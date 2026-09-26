#!/bin/bash
# Wait until Copilot has reviewed the current head of each listed PR.
#
#   scripts/dev/wait_copilot.sh 90 91 93        # checks once a minute, gives up after an hour
#   WAIT_MINUTES=120 scripts/dev/wait_copilot.sh 95
#
# A PR counts as reviewed when its head on GitHub equals the tip of its branch in the
# repository the PR comes from, a fork included (GitHub lags a push by up to a
# minute), and a review by copilot-pull-request-reviewer[bot] exists on that commit.
# Reviews are read with --paginate: a PR with many review rounds has more than one page.
# Request the review first:
#   gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
set -u
[ $# -gt 0 ] || { echo "usage: $0 PR [PR ...]" >&2; exit 2; }
repo=$(gh repo view --json nameWithOwner --jq .nameWithOwner)
for _ in $(seq 1 "${WAIT_MINUTES:-60}"); do
  pending=""
  for pr in "$@"; do
    read -r head_repo branch head < <(gh pr view "$pr" --repo "$repo" \
      --json headRepositoryOwner,headRepository,headRefName,headRefOid \
      --jq '"\(.headRepositoryOwner.login)/\(.headRepository.name) \(.headRefName) \(.headRefOid)"')
    tip=$(gh api "repos/$head_repo/branches/$branch" --jq .commit.sha 2>/dev/null)
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
