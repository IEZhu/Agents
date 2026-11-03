---
persona:
  agent: investigative_analyst
  skills: [skill-content-structure, skill-fact-verification, skill-forensic-process, skill-temporal-validation, skill-confidence-markers, skill-git-conventions, skill-dense-summarization]
  implants: [implant-chain-of-verification, implant-uncertainty-quantification, implant-narrative-of-thought]
---
# Close a thread without losing its work or keeping its errors

Follow [bounded execution and evidence reuse](../docs/flow-execution.md). Parse
large transcripts outside model context, return a compact inventory, and open
primary evidence for the claims under audit. Retain the independent verifier,
all report fields, current-state checks and the owner's keep-open constraint.

This is an executable instruction for an AI at the end of a conversation (a
thread): before `/clear`, before switching to an unrelated task, or when the user
asks to close it. It inventories what the thread produced, checks each result
against the current state and gives it an evidence-based verification level. It
audits the thread for its own mistakes and for how it used every installed tool,
writes a patch for each tool it missed or misused, secures unsaved work and
records one report.

The thread's own account is a lead, never proof. A long, expensive thread can
still be wrong. Its summary may predate later changes, and a compacted context
may have dropped what actually happened.

Before any transcript inspection, extract only the needed metadata with a
script and redact credentials before any model-visible tool output, not just
before persistence or the final report. Never print raw transcript records,
user messages or compacted retained context. Before emitting any native thread
or history read, including `read_thread` and `read_history`, project the result
to an explicit allowlist outside model context. For authorization checks, keep
only the target thread ID, user-message timestamp and the minimum relevant
redacted user text. Omit previews, reasoning, tool arguments and command bodies;
`includeOutputs=false` and output-length options do not redact those fields.
Never emit the raw result object. Recursively remove credential and capability
values from the allowed fields before any output or audit persistence. Use
compact status snapshots when authorization text is not needed. Apply this
boundary to inventory helpers and delegated auditors as well; give workers
sanitized evidence or explicit instructions to extract and redact before
emitting it.

The persona contributes fact-checking discipline. Its own investigation phases and
"Verdict"/credibility output format do not apply: this flow's steps and its report
in step 9 do.

## How to invoke

```text
Run flows/thread-close.md.
Run flows/thread-close.md in apply mode.
```

Through Agents-Core MCP, call `run_flow(flow="thread-close", request="mode: apply")`
and apply its `persona_activation` first. Run the flow in the session being
closed: it needs that session's tools and context. Another session can close a
thread only from the transcript and the live state, and its report must say so.

## Inputs and authority

- **Mode.**
  - `draft` (default) reads, checks and reports. Its only write of its own is the
    closing per-turn log, which step 9 makes only when a safe payload and an
    authorized destination are established; any other action needs the user's
    yes listed under Authority.
  - `apply` also writes memory items at level V2 or above, curates the closing
    history entry, and does the cleanup that step 8 verifies as safe.
- **Thread.**
  - **Claude Code.** Read the session's transcript with the inventory helper
    [`scripts/dev/thread_inventory.py`](../scripts/dev/thread_inventory.py) from
    the Agents-Core checkout. Run it as
    `python "$AGENTS_CORE_ROOT/scripts/dev/thread_inventory.py" --session <id>`.
    - The id is the directory name above the session's scratchpad.
    - `--project-dir <dir> --latest` lists other recently active sessions;
      confirm the right one before using it.
    - The helper also reads the transcripts of the session's subagents and
      workflow agents and marks their items with `by`.
    - Its `prompts` list contains timestamps only, without user message text or
      previews; use the sanitized primary-evidence procedure above when text is needed.
  - **Finding the helper from another repository.** Derive `AGENTS_CORE_ROOT` as
    the [pr-review helpers](pr-review.md#optional-github-helpers-from-the-agents-core-installation)
    describe:
    - when `flow.source` is `builtin`, the parent of `flow.source_path` is the
      checkout's `flows/`;
    - for a personal copy, use the `builtin:thread-close` entry of `list_flows()`.

    Do not look for `scripts/dev/` in the target repository.
  - **Other clients.** They keep their own logs; Codex, for example, keeps
    rollouts under `~/.codex/sessions`. The helper does not read those. Read them
    directly when available, otherwise reconstruct the thread from the
    conversation and the live state. State in the report which source you used.
- **Scope.** Every repository and system the thread touched; the user can narrow it.
- **Authority.** Invoking the flow allows reading everything the thread touched
  and, in `apply` mode, the writes named above. Everything else needs the user's
  explicit yes in this session:
  - commits and pushes;
  - creating or closing issues and pull requests;
  - stopping background work in `draft` mode;
  - deleting anything that step 8 does not verify as safe;
  - writing to external systems (trackers, chat, schedulers); each such write
    also passes the write-time recheck in Confidentiality and destination
    visibility;
  - memory below V2;
  - the patches that step 4 lists (flows, flow personas, user rules, client
    instructions and hooks, Agents-Core configuration, other tools'
    instructions), except a V2 memory lesson in `apply` mode.

  Never merge, force-push or rewrite history as part of closing.

## Confidentiality and destination visibility

Before external lookup, drafting a proposal or choosing a patch destination,
classify the source information and the destination's actual visibility and
audience. Internal or unknown information stays private unless the user has
explicitly authorized disclosure of those specific details to that destination.
Read-only searches and API queries also transfer their query text to the
receiving service; check this boundary before making the call.

Internal information includes nonsecret service and tool names, proxy aliases,
domains, endpoints, repository paths, API contracts, deployment configuration,
architecture and incident details. Removing credentials alone does not make
this material public. Existing public mentions do not authorize further
disclosure. Access to a private repository does not authorize publication to a
public repository, including another repository owned by the same user.

For an upstream/public improvement, describe only the general mechanism and use
synthetic examples that do not identify the organization or its systems. Keep
company-specific registrations, tool inventories and examples in an authorized
private repository, private flow or local configuration. Never propose putting
them in a public catalogue merely because the framework or catalogue lives
there. Public proposals and drafts must already be safe before asking approval.

Search for existing issues/PRs only inside an authorized destination and with
query terms safe for that destination. Do not send internal names or identifiers
to a public issue/code search to discover whether someone has already published
them. Use an authorized private tracker or a generic sanitized public query
instead. If visibility or safe disclosure cannot be established, stop only that
external operation, keep the finding private and report the verification gap.

Immediately before every external write, including a push or submission of an
approved patch, recheck the destination's actual visibility and audience and
reclassify the payload. If any differs from the approved scope or is unknown,
stop only that operation, keep the finding private and report the verification
gap. Revalidate the disclosure and its authorization before continuing; an
earlier proposal check or approval is not a substitute for this write-time check.

Apply this boundary to reports, logs, memory, links, attachments, worker inputs
and delegated searches as well. A worker receives no broader disclosure
permission than the parent. A generic instruction to run/apply this flow,
check issues, fix a workflow or "go ahead" is not permission to publish internal
details. Follow an explicit user authorization for a specified disclosure;
otherwise continue with the private or sanitized alternative.

## Verification levels

| Level | Meaning | Typical evidence |
|---|---|---|
| V0 Claimed | Only the model's words | "tests pass" with no command or output found |
| V1 Read | A source was opened and supports it | file content, documentation, an API response |
| V2 Executed | A command, test or call produced the result | the command and its output, with time and commit |
| V3 Independently checked | A separate reviewer checked the current state and found no contradiction: fresh-context agent, review bot or human | review or verifier output tied to the current head or state |
| V4 Accepted | The claim is at V2 or above, and the owner merged or applied it and it held afterwards | merge commit plus the V2 evidence, a running service, the owner's use |
| ✗ Refuted | Counter-evidence shows it wrong, during the thread or in this audit | the counter-evidence |
| ⚠ Stale | Was supported, but the subject changed afterwards | a test run before the last commit, a review of an older head |
| ? Contested | Evidence conflicts, or a check could not be completed | both sources, or what blocked the check |

Rules:

- **Evidence comes from the close, not from narration.**
  - Assign each level from evidence seen during the close, or from evidence found
    in the transcript with its time.
  - Narration alone never raises a level: "tests pass" in the thread is V0 until
    the command and its output are found, or the tests are re-run.
  - Reports that subagents and reviewers gave during the thread are narration
    too, until their output is found. They reach V3 only for the head or state
    they actually reviewed.
- **One-line justification.** Name the command, file, pull request state or
  review behind every level, with its time or commit.
- **Doubt lowers the level; only counter-evidence refutes.** When unsure between
  two levels, take the lower. An item becomes ✗ only through evidence against it.
  A check that could not confirm an item leaves its level unchanged, or makes it `?`.
- **Same scale as the skills.**
  - V1 and V2 match HIGH in `skill-confidence-markers` ("checked this turn") and
    `VERIFIED` in `skill-fact-verification`: a primary source opened this turn.
  - V3 adds independence; V4 adds the owner's acceptance.
- **Preferences and lessons.**
  - A preference the user stated has the user's own words as primary evidence:
    record it as the user's statement, with the prompt's time. A V2 memory may
    hold it.
  - A lesson may go to memory only when the error it comes from is evidenced at
    V2 or above: a command, its output or a review shows the error happened.
- **Provenance is recorded, never scored.** Record the models, number of
  responses, token totals, session id, time span and client. They describe effort
  and capability, not correctness, and never set a level. At most they decide
  what to review first: output of a small model, or a long correction loop.
  Separate ordinary/compaction requests and uncached input/cached input/output
  when the client provides them; deduplicate response IDs, never add cumulative
  counters, and disclose unavailable worker totals. A token total is not a
  billing receipt. Do not reparse or reprint the full transcript for every claim.

## 1. Establish the scope

1. Read the current time with `date -u`. Fix the mode and identify the thread.
2. In Claude Code, run the inventory helper. Keep its `notes`: other active
   sessions in the same project mean you must confirm which session is being
   closed.
3. Work out which repositories to check: the helper's `git_roots` plus any named
   in the conversation. For each one, collect:
   - `git status --short`, the current branch, `git stash list` and
     `git worktree list`;
   - commits that exist on no remote. The remote, not the upstream, decides
     whether work is pushed:
     - `git log --branches --not --remotes --oneline` lists them, and
       `git branch -r --contains <branch>` is empty for a branch whose head is
       on no remote branch. Both read remote-tracking refs, which show the
       remote as of the last fetch or push: a branch deleted or force-pushed
       there can still look pushed, and one pushed from another clone can look
       unpushed.
     - Confirm with the live heads from `git ls-remote --heads <remote>`, which
       changes nothing. A branch is pushed when a live head equals its head or
       contains it (`git merge-base --is-ancestor <branch> <live head>`), and
       unpushed when every live head is present locally and none contains it.
     - Otherwise, or when the remote cannot be reached, its push status is `?`
       and the report proposes `git fetch --prune <remote>`.
     - A branch whose head is the head of a merged pull request is preserved
       by that merge, even after a squash merge or when its remote branch is
       gone;
   - each branch's upstream, for information only:
     `git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads`.
     A missing upstream says nothing about pushing (`git push origin <branch>`
     without `-u`, or a `--no-track` branch), and an empty track column does not
     mean "in sync";
   - the commits the thread made.

## 2. Inventory the artifacts

Build one table with columns artifact, kind, location, made by (main session,
subagent or user), and what the thread claimed about it. Cover:

- **Code and documents.**
  - Commits, branches and stashes; `git_mutations` lists the git and `gh` writes
    the helper recognized and `files_written` the files its edit tools changed.
  - Worktrees and uncommitted or unpushed changes.
  - Pull requests, with state, head and reviews; issues, with state. Use the
    helper's `github_refs`.
- **Configuration outside git.**
  - Schedulers and cloud routines; settings and services.
  - Personal libraries, such as Agents-Core `flows/.user`; environment.
- **Knowledge.**
  - Decisions with their reasons, and options rejected and why.
  - Preferences the user stated.
  - Lessons from mistakes; open questions.
- **Memory** written during the thread: files and what changed (`memory_writes`).
- **External systems** the thread wrote to: trackers, chat, published pages,
  remote triggers. Use `external_writes`. Also inspect `unclassified_tools`:
  these are MCP calls whose names did not say whether they write. Likewise,
  `unclassified_commands` lists git and gh commands the helper could not
  classify as reads or writes.
- **Scheduled work**: wake-ups and cron jobs, from `scheduled`.
- **Session-local items**, which vanish with the session:
  - scratchpad and temporary files (`scratchpad_paths`);
  - background tasks, monitors and agents (`background_tasks`). One without an
    `ended` record may still be running.
- **Shell commands the user typed** (`user_commands`), which can change state as
  well.
- **Promises**: things the thread said it would do or check later, such as
  "after the pause I will request a review once".

The helper sees files changed by edit tools and, in `shell_writes`, the files that
shell redirections and `tee` wrote. Files that other programs change, such as
`cp`, `mv`, `sed -i` or a package manager, are covered by the git state only
inside repositories; outside them, check the paths the conversation names.
Commands that a program or script runs, such as a Python heredoc calling `gh`,
are invisible to the helper; the live git and GitHub state covers them. When
`unreadable_lines` (lines that are not JSON, such as the partial tail of a
transcript still being written) or `skipped_entries` (entries of a shape the
helper does not know) is not zero, the inventory can miss items; the report
names which counter and how many.

## 3. Verify against the current state

Check each artifact's live state:

- `git`, `gh pr view`, `gh issue view`;
- the scheduler's own read call;
- whether the file exists and what it contains;
- a cheap re-run of a check where the thread claimed a result.

Assign the level with its evidence. Mark an item ⚠ when the evidence predates a
later change. Note changes the thread did not make, such as a pull request the
owner merged. In `draft` mode, run only reads and checks without side effects.

## 4. Audit tool usage

Find which installed tools the thread used, used wrongly or skipped where an
instruction required them, why, and the patch that makes the next thread use them.
Installed tools are everything the client exposed to the thread: Agents-Core's
tools, other MCP servers' tools, skills, plugins and slash commands, and the
client's own tools. Calls from this close's own `run_flow` on are not audited.

1. **What was installed.** Take it from the session, not from a list:
   - every tool the client exposed, with the server or plugin it belongs to
     (`mcp__<server>__<tool>` in Claude Code; other clients name them
     differently), including tools whose schemas the client defers until a tool
     search loads them, and every skill and slash command it offered;
   - the servers that failed to connect, which the client reports at session
     start;
   - Agents-Core's read-only state calls `local_inference_status()`,
     `list_flows()` and `list_user_rules()`.

   A tool that was absent, disabled, not ready or on a failed server cannot be
   missed: it is `unavailable`. Without Agents-Core MCP in the session, say so,
   skip the catalogue below and audit the other tools.
2. **Usage rules.** For every installed tool, find the instructions that say when
   to use it:
   - **Agents-Core tools:** the catalogue below. An exposed Agents-Core tool that
     no row names is `uncatalogued`, a finding against the catalogue. The built-in
     catalogue names every statically registered tool and the dynamic proxy
     family. A missing row means a newer server or an outdated personal copy of
     this flow; a runtime proxy name belongs to `mcp-proxy` below. Concrete
     private aliases need not appear in an upstream/public catalogue; their
     absence alone is not a catalogue defect.
   - **Every other tool:** each loaded instruction that names a trigger for it:
     the client's instruction files (`CLAUDE.md`, `AGENTS.md`, rules files), the
     MCP server's own instructions, a skill's or plugin's description ("use
     when…"), the active persona bundle, and the user's explicit requests in the
     thread. Each becomes a row in the catalogue's form, with the slug
     `<server-or-skill>:<rule>` and the instruction's source.
   - A tool that no instruction names is `no-rule`. List it with its call count;
     it is never a finding.
3. **What was called.** List every call of an installed tool with its turn, time,
   tool and status, and for Agents-Core calls the persona descriptor. The helper
   lists no call of every tool: `logging_calls` counts the `log_interaction`
   calls and `unclassified_tools` counts other unclassified tools by name, while
   `external_writes` and `scheduled` list only write-classified and scheduling
   calls, with time, tool and actor but no status; take the other calls and the
   statuses from a short script over the transcript and do not reprint it. Count
   final answers apart from progress messages, and note each context compaction.
4. **Where it applied.** Match the thread against every row: the catalogue and the
   rules from item 2. Every finding names its trigger: the turn, command or tool
   call where the tool applied. "It might have helped" without a trigger is not a
   finding.
5. **Status and level.** Give each row one status:
   - `used`: called where its trigger occurred, following its protocol;
   - `misused`: called, but its protocol broken, such as a worker result used
     without `valid: true`, or an error result the thread did not handle;
   - `missed`: a trigger occurred, the tool was available, and no call followed;
   - `unavailable`: a trigger occurred, but the tool was not available;
   - `n/a`: no trigger.

   Level each `missed` and `misused` finding on the scale above: V2 when a
   script over the transcript shows the trigger and the absent or broken call,
   V1 when you read both in the transcript, V0 when only the compacted
   conversation supports it. An absent call needs a complete transcript from the
   trigger on: when the inventory's `unreadable_lines` or `skipped_entries` may
   fall in that window, a `missed` finding is `?`, and a `?` never becomes a
   memory lesson.
6. **Cause.** Name the likeliest cause of each `missed` or `misused` finding,
   with its evidence:
   - `not-in-context`: no instruction loaded at that point required the call
     (no persona active, compaction dropped the bundle, the flow step is silent);
   - `ignored`: a loaded instruction required it and the thread did not follow
     it;
   - `not-loaded`: the client defers tool schemas and the tool was never loaded;
   - `no-trigger`: no instruction anywhere names this situation;
   - `failed`: the call returned an error that the thread neither handled nor
     reported.
7. **Patch every finding.** Write one patch for each `missed`, `misused` and
   `uncatalogued` finding, whichever tool it concerns, at the layer of its cause:
   the exact diff with its file, or the exact call. Prefer a patch that fires by
   itself (a hook, a flow step, a flow persona, a standing instruction) over one
   that relies on the model remembering (memory, a preference).

   | Cause | Layer | Patch |
   |---|---|---|
   | `not-in-context`, after compaction | client hooks and instructions | the session readiness compact hook ([docs/session-readiness.md](../docs/session-readiness.md#persona-restore-after-compaction)), and a standing instruction to call `get_agent_context(force_reload=true)` after compaction |
   | `not-in-context`, inside a flow | the flow | a step naming the call, through `save_flow`, with `override=true` for a built-in |
   | `not-in-context`, wrong or thin persona | the flow persona | `set_flow_persona` with the missing skills or implants |
   | `ignored` | the instruction | move the requirement to the start or end of the block that carries it, or into a flow step; a memory lesson when the finding is V2 |
   | `not-loaded` | client configuration | load the tools at session start, or a client instruction to search for them before the first answer |
   | `no-trigger`, an Agents-Core tool | Agents-Core | a pull request or issue for the rule, persona or catalogue row that should name the trigger |
   | `no-trigger`, another tool | the instruction's owner | a diff to the user's own instruction file or skill; for a third-party server or plugin, an issue for its owner and a client instruction meanwhile |
   | `uncatalogued` | this flow | the missing catalogue row: a pull request for the built-in, `save_flow` for a personal copy |
   | `failed` | the failing component | an issue with the error and the call that produced it |
   | `unavailable`, though it would have helped | configuration | enable the capability or reconnect the server, such as local inference or the cooperation daemon |
   | a procedure repeated with no flow | the flow library | a new flow through `save_flow` |

   **Managed blocks.** The block between the `init_repo` markers in `CLAUDE.md`
   or `AGENTS.md` is regenerated from Agents-Core's
   `scripts/templates/routing-protocol-core.md`, which is itself generated from
   the section files in `src/engine/protocol/`. A hand edit of the block or the
   template is overwritten and fails `tests/test_protocol_text.py`. Patch the
   section file under `src/engine/protocol/` that states the rule through an
   Agents-Core pull request and regenerate with
   `python -m src.engine.protocol_text --write`; put rules of the user's own
   outside the markers.

   **Repeats escalate.** Search `read_history` for earlier closes tagged
   `#ac-missed-<slug>` or `#tool-missed-<slug>`, or naming that slug in their tool
   usage table. A repeat after an applied patch means the patch did not hold:
   write the next layer toward automatic (memory lesson, then client
   instruction, then hook, flow step or flow persona, then Agents-Core itself),
   and name the earlier close's time.
8. **One list, one yes.** Check existing tracking within the confidentiality
   boundary before adding a patch. For each proposal, name its destination,
   verified visibility and whether its content is generic or private. Public
   improvements describe the mechanism; keep concrete private inventories in
   authorized private context. Never require public enumeration of private
   tools as a completion condition. Collect every patch into one numbered list.
   Apply them only after the user's yes, which may approve the whole list or
   name items; a V2 memory lesson in `apply` mode needs none. Immediately before
   each external write, recheck visibility, audience and payload classification
   as required above, including for an approved push or submission. Stop only
   that operation when the scope changed or is unknown, keep the finding private
   and report the gap. Verify each applied patch (the diff in place, the call's
   result) and report which were applied.

**Catalogue.** The slug names the row in the report and in tags. The rows name
every statically registered Agents-Core tool; a test keeps those names complete.
The `mcp-proxy` row covers the runtime family, whose installed names must come
from the client's actual tool exposure.
The shared-proxy rows use the upstream Blincho and Plata tool names. Match the
suffix after the configured `<alias>__` prefix, such as `blincho__get_status` or
`plata__code_search`; a compatibility view may expose the suffix alone. Apply
these rows only to enabled, available upstream tools. A disabled or disconnected
upstream is `unavailable`, not `missed`; a saved proxy definition or cached
catalogue does not prove a live connection, and a catalogue entry grants no
authority.
Use the daemon-owned shared proxy, never start a separate upstream as a fallback,
and preserve `ENABLE_WRITE_TOOLS=false` unless the user explicitly authorizes a
policy change. Listing a write-capable tool does not authorize its calls or writes.

| Slug | Tools | Trigger | `missed` or `misused` when |
|---|---|---|---|
| `routing` | `route_and_load`, `get_agent_context`, `list_agents` | the first substantive request; a change of domain; the user asks which agents exist | an answer before any `SUCCESS` or `NO_CHANGE`; a new domain handled by the old persona; the agent catalogue described from memory |
| `persona-restore` | `get_agent_context(force_reload=true)` | a context compaction; answers that lost the footer or the rules | no reload after the compaction, and later answers break the bundle or a `log_interaction` is stored `unverified` |
| `persona-enrichment` | `refresh_persona_context`, `load_implants` | the task needed a skill or reasoning pattern the bundle lacked | the method was improvised while a matching skill or implant existed |
| `session-cache` | `clear_session_cache` | the owner asks to reset the server's shared caches | called to switch personas, which needs no reset and affects other sessions |
| `log` | `log_interaction` | every final answer eligible for safe, authorized logging | eligible final answers outnumber calls after excluding the privacy skips that step 9 allows; a `history_last_error` or workspace error went unmentioned |
| `history` | `read_history` | the user refers to earlier work | earlier work reconstructed from memory or asked for again |
| `flows` | `list_flows`, `run_flow` | the user names a flow, or asks for a task that an existing flow covers | the task done ad hoc while the flow existed |
| `new-flow` | `save_flow` | the same multi-step procedure done twice, or one the user wants to reuse | no flow proposed |
| `flow-library` | `get_flow`, `delete_flow`, `set_flow_persona` | the user asks to inspect, restore or delete a flow, or to change a flow's persona | flow files or personas changed by hand instead of through the tools |
| `delegation` | `get_subagent_context`, `validate_subagent_result` | any subagent, reviewer, verifier or parallel worker | a worker started without `status: prepared`, or its output used without `valid: true` |
| `artifacts` | `artifact_publish`, `artifact_manifest_create`, `artifact_manifest_get`, `artifact_read`, `artifact_manifest_verify`, `artifact_manifest_release` | several workers need the same large input, or input exceeds a worker's `max_context_chars` | large data pasted into worker prompts; a pinned artifact read incompletely; artifact-backed output accepted unverified; a manifest left active after its last consumer |
| `cooperation` | `cooperation_register`, `cooperation_send`, `cooperation_receive`, `cooperation_ack`, `cooperation_read`, `cooperation_apply`, `cooperation_wait`, `cooperation_request_status`, `cooperation_request_update` | another session or worker on the same checkout, such as sessions in the helper's `notes`; a peer's request or agreement awaiting a reply | intersecting edits with no registration or agreement; a delivery left unacknowledged; a request's processing state never reported |
| `local-inference` | `local_inference_status`, `local_context_create`, `local_context_grant`, `local_context_revoke`, `local_context_release`, `local_generate`, `local_generation_result`, `local_generation_cancel` | bulk routine completions: classification, extraction, short summaries | done by the main model while local inference was enabled and the task fit its limits; a context or grant left open after its last completion |
| `repo-context` | `describe_repo`, `write_repo_summary` | the thread's first work in a repository | the repository explored ad hoc; `needs_summary` not followed by a summary where a write was allowed |
| `user-rules` | `list_user_rules`, `get_user_rule`, `subscribe_user_rule`, `refresh_user_rule`, `rollback_user_rule`, `set_user_rule_enabled`, `delete_user_rule` | a subscribed rule applied, one with a refresh error, or the user asks to add, change, pause or remove a rule | a failing or outdated rule went unmentioned; a rule changed by editing files instead of through the tools |
| `mcp-proxy` | Runtime `<alias>__<tool>` tools exposed by Agents-Core, and client alias compatibility tools | an authorized upstream operation is requested, or the audit inventories exposed tools, client registrations or process ownership | an available dynamic tool is omitted from the inventory; an upstream operation bypasses a required shared proxy or breaks the upstream's usage rules |
| `proxy-read` | `get_data_range`, `get_settings`, `get_stats`, `get_status`, `get_today_stats`, `code_search`, `code_execute_readonly` | the user requests timer state, settings, statistics or service information within an enabled upstream's scope; follow its own usage rules | an applicable authorized read was missed; a read needlessly used write-capable execution; if an unavailable upstream was bypassed, keep this row `unavailable` and record the alternate-server startup as misuse under the tool that started it; access claimed without checking the live connection and the upstream app's state; Blincho detailed activity spans, call history or names, event IDs, journal answers or camera frames exposed instead of public settings, status or aggregate statistics; credentials or tokens exposed in output |
| `proxy-state` | `pause_timer`, `resume_timer`, `skip_break`, `start_break`, `code_execute` | the user explicitly authorizes a timer/break change or write-capable service execution within the upstream's enabled policy | a state change lacked authorization; queued timer work was reported completed without checking the resulting state; an unfinished strict break was bypassed; writes were enabled against the retained policy; credentials or tokens exposed in output; record a separate-upstream startup as misuse under the tool that started it; keep an unavailable proxy row `unavailable` |

Inventory dynamic proxy tools under their upstream connection as well as their
transport. Their runtime names are not `uncatalogued` solely because they are
absent from the static registration catalogue. Inventory concrete private names
only in authorized private context; a generic upstream rule can cover private
aliases without publishing them. Read and apply the upstream's own usage and
authorization rules. Inventorying a tool does not authorize
calling it: a `missed` call requires demonstrated availability and an applicable
operation trigger, not just exposure to the client.

## 5. Audit the thread itself

1. **Claims.** List the load-bearing claims the thread made to the user: facts,
   numbers, statuses, causes and recommendations, mainly from its final answers.
2. **Corrections and contradictions.**
   - Read the helper's `corrections`: they are candidates, not conclusions.
   - Find decisions the thread reversed and statements contradicted by later turns.
   - The claims they invalidate become ✗, with the later evidence.
3. **Fresh-context verifier.** Add the `missed` and `misused` findings of step 4 to the claim list, with their transcript references. Call `get_subagent_context` for a suitable canonical role with `result_type="research"`, the claim list, source references, a read-only scope and criteria covering every claim. Require `status: prepared` and launch a fresh native worker with the receipt's `launch` text and the sources, without the conversation. Ask it to classify each claim as confirmed, refuted with counter-evidence, or unconfirmed with the blocker, within the prescribed result format. Call `validate_subagent_result` with the receipt's `contract_revision` and its unchanged output (`result_ref`, or `result` when no file holds it); require `valid: true` and check the evidence before using its conclusions. Follow the [delegation contract](README.md#delegated-workers) for invalid or unavailable results.
   - Refuted claims become ✗.
   - Unconfirmed claims keep their level, or become `?`.
   - A claim reaches V3 only if the verifier actually checked its sources.
   - Without a prepared worker and a validated result, re-check the most important claims yourself and say that no validated independent verification completed. Nothing then reaches V3 through this step.
4. **What the thread got wrong.** List each error, its effect, whether it was
   fixed, and the lesson.

## 6. Secure the work

- **Uncommitted or unpushed changes.** For each, propose a commit and push to the
  right branch under the repository's rules, or an explicit drop. Never decide
  silently; act only on a yes.
- **Scratchpad and temporary files.** Name what is still needed (drafts, plans,
  generated data) and propose a lasting place for it: repository documentation, an
  issue, memory or a published page. The rest is disposable.
- **Background and scheduled work.** List what still runs and who consumes its
  output. In `apply` mode, stop tasks, monitors, wake-ups and cron jobs whose
  purpose has ended; in `draft` mode, propose it.
- **Promises and open loops.** Give each an owner and a trigger: an issue or
  tracker item (on a yes), a follow-up command, or an explicit "dropped".
- **No new stores.** Use the existing ones.

## 7. Persist durable knowledge (`apply`)

- **Memory.** Only durable items at V2 or above, as defined above: preferences,
  lessons, and project facts the repository does not already record.
  - Update an existing memory instead of adding a duplicate, and give each item
    its date and level.
  - Items below V2 go to a "to verify" list, each with the check that would
    verify it.
  - If the client has no durable memory, say so.
- **Tool-usage patches.** A `missed` or `misused` finding at V2 may become a
  memory lesson, phrased as its trigger and the call it requires. The other
  patches from step 4 wait for the user's yes; a patch to Agents-Core itself
  goes out as a pull request or issue.
- **Issues, tracker items and documentation.** Propose follow-up issues and
  tracker items, and create them only on a yes. Propose repository knowledge as a
  documentation change through the repository's pull request flow.
- **Confidential information, secrets and personal data.** Check the authorized
  audience of every destination, including the closing log and any synced
  memory. Scan everything persisted or reported and replace secrets with safe
  references, including in private memory and closing logs. Keep internal
  identifiers, topology and nonsecret configuration out of public/shared output
  unless specifically authorized. Sanitize references and link labels as well
  as prose; leave out medical, financial and other personal details. In both
  modes, log the report only when a safe payload and authorized destination are
  established. Otherwise, report the privacy skip and incomplete closing-log
  step as required by step 9.

## 8. Clean up (`apply`)

Remove a worktree only when all of these hold:

- `git status --short --ignored` lists nothing except regenerable caches
  (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`). `git worktree
  remove` deletes ignored files such as `.env`, data and logs.
- Its branch head equals the head of a merged pull request, or the default
  branch contains it.
- It is not the current working directory, the MCP workspace root or the
  Agents-Core installation.
- No other session, open task or monitor uses it.

Delete a local branch only under the same branch condition. When any condition is
uncertain, list the item instead of removing it.

## 9. Report and close the turn

Compose the report in the user's language:

1. **Summary** in three to five lines: what was achieved, and how well it is
   supported overall.
2. **Artifacts:** a table of artifact, location, made by, level and evidence.
3. **What this thread got wrong.**
4. **Tool usage:** a table of slug, status, trigger, cause, level and patch for
   every row that is not `n/a`, catalogue rows and other tools' rules alike; then
   the `uncatalogued`, `no-rule` and `unavailable` tools, the earlier closes with
   the same finding, and the numbered patch list awaiting a yes.
5. **Unsaved work and decisions needed**, each with a proposed action.
6. **Open loops**, each with owner and trigger.
7. **Persisted** (`apply`) or **would persist** (`draft`): memory changes and
   cleanup.
8. **Provenance**, labelled as such: models, responses, tokens (main session and
   subagents), session, time span, transcript source.
9. **How to resume:** session id or link, branch, next command.

At the final handoff, use `finishing` (🧹) when required obligations remain,
unless essential user input or required human approval calls for `needs_user`/`review`.
Finish any independently authorized work before that handoff. Resume implementation as `working`;
AI review, CI and subagents use `working`/`waiting`.
Verify or explicitly resolve every required task before `ready_to_close`;
an owner, tracker item or proposed follow-up alone does not resolve it. A completed
audit report does not mean the audited work is complete, and optional suggestions
alone do not require `finishing`. `closed` requires explicit closure or confirmed archival.
The status grants no additional authority; follow **Inputs and authority** for each action.

Then, only when a safe payload and authorized destination are established, make
this turn's single `log_interaction` call under the client protocol. For a
policy-authorized privacy skip, report the gap and leave this closing-log step
incomplete; do not classify that skip as a missed or misused call. Do not make
a second call:

- `agent_name` and `persona`: the active persona;
- `persona_action`: `switch` when the flow's `persona_activation` returned
  `SUCCESS` in this turn, `keep` when it returned `NO_CHANGE` or `ERROR`;
- `query`: the current user's request after applying the sanitization rules above;
  keep verbatim content only when it is safe for the authorized closing-log audience.
  If a required logging contract demands unsafe verbatim content, do not persist
  it: report the conflict and leave this closing step incomplete;
- `response_content`: the report exactly as it will be delivered, footer
  included, but without the time line that the `answer-timestamp` rule puts
  first once this call returns.

In `apply` mode, also curate the call:

- `intent`: `Close thread: <title>`;
- `action`: `Flow: thread-close (apply)`;
- no `outcome`: the history entry then keeps the whole report from
  `response_content`, with its artifacts, errors, open loops and how to resume;
- `files`: the artifact paths;
- `tags`: `#thread-close`, `#session-<id>`, and for each `missed` or `misused`
  finding at V1 or above `#ac-missed-<slug>` (a catalogue row) or
  `#tool-missed-<slug>` (another tool's rule).

`history.md` is append-only. Before closing a thread again, look for its earlier
close with `read_history`, which returns recent or similar entries without a
session filter. Accept an entry only when its tags contain both `#thread-close`
and this thread's `#session-<id>`, and name that entry's time in the report. If
none matches, the report says that no earlier close was found. Without
Agents-Core MCP, or after a privacy skip under step 9, this close creates no
history entry; say so in the report.

**Hand-off keys.** List the session id, pull request and issue numbers, and
branch names in the report, so that daily summaries such as a personal day-close
flow can match this work instead of recording it twice.

## Done when

- Every artifact has a level with evidence, or is marked unverified.
- Every installed tool is covered by a catalogue row or another tool's rule, or
  reported as `uncatalogued`, `no-rule` or `unavailable`; every row has a status;
  every `missed`, `misused` or `uncatalogued` finding has a trigger, a cause, a
  level and a patch in the list.
- The audit ran with an independent verifier, or the report says why not.
- No unsaved work is left without a decision.
- Background work is stopped or its continuation is justified.
- In `apply` mode, every memory change is V2 or above.
- In `apply` mode, the closing log entry is curated, or the report names the
  privacy skip, the unavailable Agents-Core MCP or the conflict that left the
  closing-log step incomplete.
- Every external lookup and proposed patch destination respects the
  confidentiality boundary; no public enumeration of private tools is required.
- Every external write performed by this flow has a recorded passing visibility,
  audience, and payload recheck. A skipped, failed, or unknown recheck blocks the
  affected write and is reported; it cannot be counted as a completed action.
- The report is delivered.

The flow does not prove a thread right. It records how well each result is
supported at the moment of closing, and a later change can make that stale.
