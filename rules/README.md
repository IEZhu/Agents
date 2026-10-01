# Rules Directory

Universal rules are short, always-on directives that every agent receives. They
set the floor for honesty and fit. Guidance that depends on the agent, domain or
register belongs in a [skill](../skills/README.md) listed in the agent's
`core_skills`, `preferred_skills` or `capable_skills`.
[`src/engine/rules.py`](../src/engine/rules.py) loads only `rules/rule-*.mdc`;
this README is not a rule.

## Active rules

In load order:

| Priority | Name | File | Description |
|---:|---|---|---|
| 10 | `no-fabrication` | [rule-no-fabrication.mdc](rule-no-fabrication.mdc) | Confirm load-bearing specifics this turn, or mark them. |
| 15 | `serve-the-request` | [rule-serve-the-request.mdc](rule-serve-the-request.mdc) | The request outranks persona and skill defaults. |
| 20 | `honest-uncertainty` | [rule-honest-uncertainty.mdc](rule-honest-uncertainty.mdc) | Calibrate confidence on judgment calls. |
| 30 | `anti-sycophancy` | [rule-anti-sycophancy.mdc](rule-anti-sycophancy.mdc) | Don't agree to agree. Push back on errors. |
| 90 | `language-match` | [rule-language-match.mdc](rule-language-match.mdc) | Match the language of the last message. |

The rule files are authoritative. Update this table when a rule is added,
removed, renamed or reprioritized.

## Loading

- Every protocol 2 persona bundle includes all rules, whatever the agent or tier,
  in `rules_block`, which follows `persona_block` and precedes the skill and
  implant blocks. There is no semantic retrieval or index. The block states that a
  rule wins where persona, skill or implant text conflicts with it. The descriptor's
  `rules_loaded` and the footer's `Rules` list the rule names. The footer's muted
  `Agents-Core <version>` segment follows the labelled lists and is not a rule.
- Rules are sorted by `priority` (ascending), then by `name`.
- Bundle assembly re-reads the files for every bundle in strict mode. An invalid
  rule, a duplicate `name` or an empty rule set makes every activation return
  `ERROR`. The shared daemon runs the same check during warmup and does not become
  ready when it fails. The per-query enrichment path used by the evaluation harnesses
  skips an invalid file with a logged error and keeps the loaded set for the life
  of the process.
- `RULES_ENABLED=0` in the environment or the installation `.env` disables the
  layer for diagnostics: bundles carry an empty `rules_block`, and the footer shows
  `—` for `Rules`. See [environment variables](../README.md#environment-variables).
- The local web UI (`python -m src.daemon flows-ui`, tab *Rules*) can switch single
  rules off for the whole installation. A switched-off rule leaves persona bundles
  and the footer; its file is untouched, and switching every rule off gives an empty
  `rules_block` like `RULES_ENABLED=0`. The per-query path used by the evaluation
  harnesses ignores the switches. See [Flow editor](../docs/shared-mcp-daemon.md#flow-editor).

## Frontmatter

```yaml
---
name: no-fabrication    # Required, unique; letters, digits, "_" or "-"
description: Confirm load-bearing specifics this turn, or mark them.  # Text, shown under the rule heading
priority: 10            # Integer, default 50; lower values load first
category: accuracy      # Text, default "general"; not used for loading or rendering
---
# No fabrication
Rule body (required). A leading H1 is dropped when rendered under "### Rule: <name>".
```

`applies_to` and `exclude_agents` are rejected: a directive that should not reach
every agent is a skill.

## Adding or editing a rule

1. Name the file `rule-<name>.mdc` after its `name` field. Keep the text short:
   every bundle carries every rule in full.
2. Adding, removing or renaming a rule changes every agent's behavior. Update
   `_EXPECTED_RULE_NAMES` in [`tests/test_rules.py`](../tests/test_rules.py) in
   the same commit. `rule-no-fabrication.mdc` must also keep the phrases in
   `_NO_FABRICATION_LOAD_BEARING_PHRASES` and stay byte-identical to the A/B
   fixture `evals/fixtures/rule-no-fabrication.compressed.mdc`.
3. Run `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_rules.py -q`.
4. No reindex or daemon restart is needed. The installation that serves the client
   reads the edited files when it builds the next bundle: a new activation, a
   `force_reload` restore or `refresh_persona_context`. The bundle revision covers
   the rules text, so a refresh returns `SUCCESS` with the new rules instead of
   `NO_CHANGE`; a local `keep` retains the rules the client already applied. See
   [routing](../docs/routing_flow.md) for the refresh contract.
