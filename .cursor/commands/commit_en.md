# /commit_en

Commit all changes with an English message.
See rules: `.cursor/rules/10-tech-writer.mdc`
See rules: `.cursor/rules/00-router.mdc`
See rules: `.cursor/rules/99-environment.mdc`

Profile: **Tech Writer**
Action:
1. Run `git add .cursor/ docs/` to explicitly stage architecture and config changes.
2. Run `git add .` to stage all other changes.
3. **🛡️ Large File Guard**: Run the following check on staged files:
   ```bash
   git diff --cached --name-only -z | xargs -0 -I{} sh -c 'sz=$(git cat-file -s ":$(printf "%s" "$1")" 2>/dev/null || echo 0); if [ "$sz" -gt 52428800 ]; then echo "⚠️  LARGE FILE: $1 ($(echo "$sz" | numfmt --to=iec-i))"; fi' _ {}
   ```
   - Threshold: **50 MB** (GitHub rejects files >100 MB; warn early).
   - If ANY large files are found: **STOP**, list them, and ask the user how to proceed (unstage / add to `.gitignore` / use Git LFS).
   - Do NOT proceed to commit until resolved.
4. Run `git diff --cached` to inspect staged changes.
5. Generate a concise Conventional Commits message in **English**, highlighting architecture changes if present.
6. Run `git commit -m "..."` with the generated message.
