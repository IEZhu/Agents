## Review discipline

This section states how review findings are handled in this repository. The
matching review configuration is `.coderabbit.yaml`, which points CodeRabbit at
this file through `knowledge_base.code_guidelines`; an AI client that loads this
file reads these rules too. Both files are installed by
`scripts/install_review_config.py` in the Agents-Core checkout.

### Answer every finding in its own thread

Reply in the thread the reviewer opened, in concise English, with ordinary
punctuation and no em dashes. Do not use an en dash to separate parts of a
phrase; it stays available for numeric ranges and other established notation,
which a repository style rule may also require. State the conclusion first. Do
not rate, thank or praise the finding, and do not answer several findings in one
comment.

- `Fixed in <sha>. <What changed, and the evidence.>`
- `Declined: <why it is not a defect, from the current source.>`
- `Deferred to <owner/repo>#<issue>: <why it does not belong in this change.>`

A finding with no reply is an open finding. Never close a thread by pushing a
commit without saying which commit addressed it.

### Fix in this change, or defer deliberately

Fix in this pull request:

- a blocker or a finding the reviewer rates major or higher;
- any security or privacy finding;
- anything that can silently corrupt data or results, whatever its label;
- a correctness or risk finding in code that ships or runs for users,
  including installers, scripts and migrations;
- a regression that one of this change's own fixes introduced.

A finding that only suggests a refactor is the exception to that list, in
shipped code as well: defer it rather than widening this change. Defer only
with an issue reference, and that also covers a feature request and work that
needs a decision this change cannot make.

### Prove the fix

A behavioural change takes a regression test that reproduces the exact
condition the reviewer named, not the happy path again. The condition is the
test: a CRLF file, a platform where a stat field reads zero, one malformed entry
among valid ones, a result set larger than one page, an empty environment
variable, a clock the test froze. State in the thread which test now fails
without the fix.

A documentation, metadata or configuration fix often has no runtime condition.
Prove it with a deterministic check of the text or the value instead: the parser
that now accepts the file, an assertion on the rendered string, a command whose
output changed. Where neither a test nor such a check can exist, say so in the
thread and name what you did verify.

Run the checks that cover the changed contract and report their real result,
including a check you could not run and why.

### Keep the description true

The description must describe the diff that is in the branch now. A statement
that only one field changed, while the body of a file changed as well, is a
defect in the change, and reviewers report it as one. Update the description in
the same push as the code.

### Converge the cycle

Request a fresh review for the new head after each round of fixes. Do not merge
while a thread is unanswered, while the reviewed head is older than the branch
head, or while a required check has not reported.

### Self-review before asking for a review

These classes produced most findings in this owner's repositories. Walk them
before pushing.

1. Two code paths, surfaces or documents that must agree: an endpoint that
   previews and the endpoint that accepts, a handler and its template, a
   runtime instruction and the document that states it.
2. An absolute requirement with no exception branch, next to a case where it
   cannot hold.
3. A required step that cannot be completed with the data at hand, so a value
   gets invented.
4. Boundary input: negative, zero, empty, absent, far larger than expected, or
   valid in syntax and impossible in meaning.
5. Error handling that fails open: one bad entry aborting an enumeration, a
   failure turned into a warning while the caller reports success, a swallowed
   exception with no log.
6. A value interpolated into another format without that format's escaping:
   shell, sed replacement, SQL, configuration files, URL path segments, HTML,
   regular expressions.
7. A deadline or budget advertised but not passed on to the calls it covers, so
   the promised limit cannot hold.
8. An irreversible action with no verified rollback point, and a deletion
   allowlist that matches by prefix instead of a known inventory.
9. A decision that depends on inherited environment or user-level tool
   configuration, which the caller does not control.
10. Secrets or user text reaching logs, process environment, command arguments
    or diagnostic output, including masking that stops at the first whitespace.
11. A write that leaves the repository or account without its destination and
    payload audience being rechecked at the moment of the write.
12. A period-scoped number computed from a cumulative row or an unfiltered
    aggregate.
13. A query or scan that grows with total stored data and runs per request, and
    reliance on an external tool's default page size or default filters.
14. Documentation, metadata, help text or a docstring that the same change made
    untrue.
