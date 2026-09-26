I can't know what your lead was thinking, but the message I gave you has problems, and the biggest one is not the format. **The commit mixes two unrelated changes**: a real performance fix and a 14-file prettier pass. I should have pointed that out the first time instead of writing a message that covered both.

## What's wrong

**1. One commit, two changes (most likely the reason it was bounced)**
- **Review noise:** the dedupe change is maybe a dozen meaningful lines hidden in a 14-file whitespace diff. The reviewer can't easily confirm that the other 13 files have no behavior changes.
- **Revert/bisect:** if the Set-based dedupe causes a bug, reverting it also reverts the formatting, and probably conflicts with everything built on top of it.
- **Blame:** every reformatted line now points at a "perf" commit. A separate style commit can be added to `.git-blame-ignore-revs`. A mixed commit can't.

**2. The subject line breaks common conventions**
- `Replaced` should be imperative: `replace`. The subject finishes the sentence "this commit will…".
- Drop the trailing period.
- If your repo runs commitlint with `@commitlint/config-conventional`, a subject in sentence case ("Replaced …") and one ending in a full stop are both rejected by default, as far as I recall. Check your `commitlint.config.*` if you have one.

**3. The body wasn't wrapped.** It was one long line. Wrap it at about 72 characters so it reads properly in `git log` and terminal tools.

**4. A detail I made up.** I wrote "tracks seen ids", but you never said what the dedupe key is. Correct it if the key is something else.

## How to split it

Since nothing is pushed, redo it as two commits, **formatting first**, so the perf diff contains only the logic change:

```bash
# 1. Undo the commit but keep your changes in the working tree
git reset HEAD~1

# 2. Check that dedupe.ts is the ONLY file with non-formatting changes
git diff src/search/

# 3. Keep your new dedupe.ts somewhere safe
cp src/search/dedupe.ts /tmp/dedupe.ts.new

# 4. Restore the original files and reformat them (same prettier version/config as before)
git checkout -- src/search/
npx prettier --write src/search/
git add src/search/
git commit   # style message below

# 5. Put your dedupe change back on top
cp /tmp/dedupe.ts.new src/search/dedupe.ts
npx prettier --write src/search/dedupe.ts
git add src/search/dedupe.ts
git commit   # perf message below
```

Warning: step 4 throws away every uncommitted change under `src/search/`. That's safe only if step 2 confirms the other 13 files have nothing but formatting changes. If they have more, save those files too before you continue.

Afterwards, `git show --stat HEAD~1` should list about 14 files, and `git show HEAD` should show only `dedupe.ts`.

## Messages

Commit 1:

```
style(search): format src/search with prettier

The folder had drifted from our prettier config. Formatting only,
no behavior changes.
```

Commit 2:

```
perf(search): replace quadratic dedupe with Set-based lookup

The duplicate check in dedupe.ts was O(n^2) and dominated /search
latency on large result sets. It now tracks seen keys in a Set,
making the check O(n).

p95 on /search for the 20k-item fixture: 840ms -> 95ms.
```

If your team uses `.git-blame-ignore-revs`, add the style commit's SHA to it in a follow-up once it's merged. It's also reasonable to ask your lead whether they'd rather have the formatting pass as a separate PR entirely. Some teams prefer that.
