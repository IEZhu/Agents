# Review configuration templates

Source files for [`scripts/install_review_config.py`](../../install_review_config.py),
which installs a CodeRabbit configuration and the review-discipline section into
a target repository. The reference for operators is
[docs/review-config.md](../../../docs/review-config.md).

| File | Role |
|---|---|
| [coderabbit-base.yaml](coderabbit-base.yaml) | Defaults for the generated `.coderabbit.yaml`, loaded as data. Holds only keys with a deliberate reason to differ from CodeRabbit's own defaults. |
| [review-discipline.md](review-discipline.md) | The managed section the installer injects into the target's `AGENTS.md`, between HTML-comment markers. |
| [path-instructions/universal.yaml](path-instructions/universal.yaml) | Defect classes that recurred in every stack. Always installed first. |
| `path-instructions/<stack>.yaml` | Guidance for one stack. The file name is the stack name the installer detects and that `--stacks` accepts. |

## Editing a fragment

Each fragment is a YAML sequence whose entries carry exactly `path` and
`instructions`, matching `reviews.path_instructions` in CodeRabbit's
configuration schema v2. An instruction must name a checkable condition: what
the reviewer inspects, what makes it a defect, and the fix direction. Keep the
prose English with ordinary punctuation, and keep every instruction under the
schema's 20000-character limit.
`tests/test_install_review_config.py` checks those properties for every shipped
fragment, so a malformed addition fails the suite rather than a review. That
file runs in the regular `pytest tests/` suite; no path-filtered workflow
runs it yet, so run it locally after editing a fragment.

## Adding a stack

1. Write `path-instructions/<stack>.yaml`.
2. Add the stack's file suffixes to `STACK_SUFFIXES` in the installer, and an
   entry in `STACK_MIN_FILES` when one file should already select it.
3. Run `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest
   tests/test_install_review_config.py -q`.
4. Record the stack in [docs/review-config.md](../../../docs/review-config.md).

Instructions are grounded in findings that CodeRabbit, Copilot and Cursor
actually reported in this owner's repositories, not in general style advice. A
new instruction needs that kind of evidence behind it; a linter rule does not
belong here, because every CodeRabbit static-analysis integration is already
enabled by default.
