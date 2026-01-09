# /route — Agent Profile Routing

Analyze my last request and select **one** profile from the available ones (see `.cursor/rules/00-router.mdc`):
See rules: `.cursor/rules/00-router.mdc`
See rules: `.cursor/rules/99-environment.mdc`
- Security Expert
- Bio-Hacker
- Psychologist
- Investigative Analyst
- Semantic Expert
- Universal
- Software Engineer

Then:
1) **Form a Header** (see `.cursor/rules/05-implant-router.mdc`):
   `Profile: <TYPE> | Stack: <A/B/C/D> | Addons: <Names>`
2) Pull context **explicitly** via `@...` (see corresponding rules in `.cursor/rules/10-*.mdc`).
3) Answer strictly in the selected persona/template.

Constraints:
- Response language follows user rules (default: English).
- If the domain is unclear — ask 1–3 clarifying questions and suggest 2–3 profile candidates.
