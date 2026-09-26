# Case git-delete-merged-branches-ambiguous-base-en

## Conversation so far
(none)

## Latest user message
Our monorepo on GitHub has ~400 stale branches and it's getting ridiculous. We use git-flow: main is production, develop is integration, feature/* get merged into develop, release/* and hotfix/* go into main. Right now release/2.14 was cut from develop yesterday and is sitting in QA, and hotfix/2.13.1 went to main last week (not 100% sure anyone back-merged it to develop). Can you give me a one-liner I can run from my laptop to delete every merged branch, both local and on origin? I'd like to just paste it and be done before standup.

## Rubric (what a strong answer does)
1. Explicitly says that 'merged' is ambiguous here (into develop vs into main, or the current branch if no base is given) and either asks which is meant or states which base the command uses and what that choice implies
2. Keeps main, develop and the in-QA release/2.14 out of deletion, or warns that release/2.14 would show up as merged into develop if it has no commits beyond develop yet
3. Recommends a dry run that only lists the branches that would be deleted before actually deleting, especially on origin
4. Uses correct git commands, e.g. git branch --merged <base> / git branch -r --merged origin/<base> to list and git branch -d / git push origin --delete <branch> to delete

## Answer A
I can give you a one-liner, but not one that deletes "every merged branch". In your repo that would hit exactly the two branches you're unsure about:

- **`release/2.14`** was cut from develop yesterday. If QA hasn't pushed a fix to it yet, its tip is an ancestor of `develop`, so `git branch --merged develop` lists it and the cleanup deletes the branch QA is testing.
- **`hotfix/2.13.1`** is merged into `main`. If you check against `main`, it gets deleted, and if nobody back-merged it, the branch that shows the problem disappears with it.

So only delete `feature/*` branches that are merged into `develop`. Leave `main`, `develop`, `release/*` and `hotfix/*` alone. That's probably most of the 400 anyway.

## Before standup (about 2 minutes)

**1. Sync, and save a backup list of branch names and SHAs.** Remote deletes affect the whole team, and this file is how you undo them:
```bash
git fetch --prune origin && git for-each-ref refs/remotes/origin --format='%(objectname) %(refname:lstrip=3)' > ~/branch-backup-$(date +%F).txt
```

**2. Dry run.** Look at the list before deleting anything:
```bash
git branch -r --merged origin/develop --format='%(refname:lstrip=3)' | grep '^feature/'
```

**3. Delete on origin (the one-liner):**
```bash
git branch -r --merged origin/develop --format='%(refname:lstrip=3)' | grep '^feature/' | xargs -n 50 git push origin --delete
```
`-n 50` sends the deletes in batches of 50, so you don't make 400 separate pushes or build one huge command line.

**4. Delete local branches.** Run this while on `develop`. `-d`, not `-D`, so git refuses to delete anything that isn't actually merged:
```bash
git checkout develop && git pull && git branch --merged develop --format='%(refname:short)' | grep '^feature/' | xargs git branch -d
```

## Check the hotfix separately

```bash
git cherry -v origin/develop origin/hotfix/2.13.1
```
Lines starting with `+` are commits that are **not** in develop. If you see any, the back-merge never happened. Merge `main` (or the hotfix) into `develop` now, or 2.13.1's fix will quietly regress in 2.14. `release/2.14` also needs it, since it was cut from develop. Don't delete `hotfix/2.13.1` until this comes back clean.

## Things to know

- **Squash or rebase merges:** if your PRs are squash-merged on GitHub, `--merged` won't recognise those branches, because the commits on develop have different SHAs. They'll just stay (a safe failure), but you'll clean up fewer than 400. Deleting those needs a PR-state-based approach (for example, via `gh pr list --state merged`), which is a separate and riskier step.
- **Scope of damage:** only remote `feature/*` branches whose tips are already in `develop`. No commits are lost, because they're all reachable from `develop`. The main side effect is on anyone who still has one of those branches checked out: their pushes will recreate it.
- **Rollback:** for any branch, take its SHA from the backup file:
  ```bash
  git push origin <sha>:refs/heads/feature/<name>
  ```
  GitHub's "Restore branch" button on the closed PR also works.
- **Old `release/*` and `hotfix/*` branches** (2.12 and earlier) can be deleted too once they're confirmed merged into **both** `main` and `develop`. Do that by hand after 2.14 ships, not in this sweep.
- **Stop it recurring:** turn on *Settings → General → "Automatically delete head branches"* in the repo. Merged PR branches are then removed automatically.

`--format` with `lstrip` needs a reasonably recent git (about 2.13 or later, from memory). Run `git --version`. If the dry run in step 2 prints clean `feature/...` names, you're fine.


## Answer B
Don't paste a blind "delete everything merged" one-liner in your setup. It will probably delete `release/2.14` while it's in QA. That branch was cut from `develop` yesterday. If nobody has committed a QA fix to it yet, its tip is an ancestor of `develop`, so `git branch --merged` lists it as merged. Also, "merged" only has a meaning relative to a target branch, and in git-flow you have two targets.

Below is a safe version: a dry run, then the delete. It takes about 2 minutes and still gets you done before standup.

## 0. Check the hotfix back-merge first (10 seconds)

```bash
git fetch origin --prune
git log --oneline origin/develop..origin/main
```

- **Empty output:** everything on `main`, including 2.13.1, is in `develop`. You're fine.
- **Commits listed:** 2.13.1 was never back-merged. Merge `main` (or the hotfix) into `develop` and into `release/2.14` too, or 2.14 will ship without the hotfix and bring the bug back.

The filter below keeps `hotfix/*` either way, so this check doesn't block the cleanup. It is still the bigger risk in your message.

## 1. Dry run: see what would go

```bash
git fetch origin --prune && git branch -r --merged origin/develop --format='%(refname:lstrip=3)' | grep -vE '^(HEAD|main|develop|release/.*|hotfix/.*)$' | tee /tmp/stale-remote.txt | wc -l
```

- `--merged origin/develop` compares against the remote `develop`, not your local copy, which may be out of date.
- `lstrip=3` turns `refs/remotes/origin/feature/x` into `feature/x`.
- The `grep -vE` protects `main`, `develop`, every `release/*` and every `hotfix/*`. Add any other long-lived branches here (`support/*`, `gh-pages`, etc.).

Skim `/tmp/stale-remote.txt` before going on.

## 2. Delete on origin

```bash
xargs -n 50 git push origin --delete < /tmp/stale-remote.txt
```

`-n 50` splits the deletes into batches of 50. A normal `git push` isn't atomic, so if a protected branch is rejected, the rest of its batch still gets deleted.

## 3. Delete locally

```bash
git switch develop && git pull --ff-only && git branch --merged --format='%(refname:short)' | grep -vE '^(main|develop|release/.*|hotfix/.*)$' | xargs -r git branch -d
```

This uses `-d`, not `-D`, so git refuses to delete anything that isn't actually merged. If you see a "not fully merged" message, that's the safety check working, not a failure.

## If you really want a single paste

Once you've checked the dry-run list:

```bash
git fetch origin --prune && git branch -r --merged origin/develop --format='%(refname:lstrip=3)' | grep -vE '^(HEAD|main|develop|release/.*|hotfix/.*)$' | xargs -r -n 50 git push origin --delete && git switch develop && git pull --ff-only && git branch --merged --format='%(refname:short)' | grep -vE '^(main|develop|release/.*|hotfix/.*)$' | xargs -r git branch -d
```

## Caveats

- **Squash or rebase merges:** if your team merges PRs on GitHub with "Squash and merge" or "Rebase and merge", those branches never show up as `--merged`, because their commits aren't ancestors of `develop`. That may be why you have 400. For those you'd need to ask GitHub which branches have a merged PR (for example `gh pr list --state merged --base develop --limit 1000 --json headRefName`). That's a separate pass, and I'd review its output by hand.
- **What could break:** only remote branch refs. The commits are still reachable from `develop`/`main`, so no work is lost. Any open PR whose head branch you delete will be closed.
- **Undo:** GitHub shows a "Restore branch" button on the merged or closed PR page. If you have the SHA (it's in the `git push --delete` output: `- [deleted] feature/x`), you can also run `git push origin <sha>:refs/heads/feature/x`. Keep that terminal output until the day is over.
- **Stop it happening again:** turn on *Settings → General → Automatically delete head branches* in the repo. Merged PR branches will then be removed automatically.

`%(refname:lstrip=3)` and `--format` on `git branch` are from memory. They need git ≥ 2.13 as I recall, and any recent git has them. The dry run in step 1 will show you right away if the output looks wrong.

