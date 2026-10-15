# Research and architecture decisions

Status: proposal based on source inspection on 2026-10-15. Baseline: `d7e34eb10cb2ab9f593ebf782a4538f677484493`. Findings describe this revision, not every existing branch or the future live daemon.

## Human and workflow requirements

The user wants agents to resolve routine intersections autonomously rather than repeatedly ask a person which agent may proceed. The target interaction is instrumental and simultaneous: the user supplies goals and constraints; agents split work and expose only material decisions or unresolved conflicts. The architecture combines a parent/worker hierarchy inside a task with peer negotiation between independent sessions. Peers do not gain authority over one another's assignments.

An intersection includes duplicate tasks, incompatible edits to the same logical area, an API producer and its consumer, shared test resources, and reuse of another agent's findings. A Git merge conflict is only one possible signal. A different branch does not make two incompatible API changes independent.

## Verified repository baseline

All paths below are relative to the repository. Confirm them again if the baseline changes.

| Source | Existing behavior | Consequence |
|---|---|---|
| `src/server.py`, `src/engine/subagent.py`, `src/schemas/subagent.py` | Worker preparation returns a stateless compact package and immutable task contract; result models forbid extra fields | Cooperation must not silently add fields to the existing contract/result envelope |
| `src/engine/subagent_validation.py`, `docs/subagent-contract.md` | Validation checks structure/identity, not factual correctness, runtime permissions or completion | An accepted peer message does not replace result validation or parent assessment |
| `src/engine/subagent.py:22-28,77-103` | Four worker rule IDs are mandatory; worker preparation also includes enabled non-presentation universal rules | A new worker cooperation rule needs a deliberate required-set change and budget checks; avoid duplicate universal/worker text |
| `src/engine/rules.py`, `rules/README.md` | Conversation rules are root-level rule files with ordering and toggles; explicit flow selection can choose an exact set | A root rule alone does not cover every flow; runtime preconditions must not depend on its inclusion |
| `scripts/templates/routing-protocol-core.md`, `CLAUDE.md`, `src/server.py`, `src/engine/persona.py` | Delegation policy is distributed in the routing template and its restatements | Update these together, rather than changing only one agent persona |
| `src/daemon/workspaces.py:36-118,130-147` | Workspace registration/context verifies local roots and rejects missing/invalid context | Use existing access checks; a peer-supplied path is not a trusted workspace |
| `src/engine/config.py:255-275` | Memory root handling can map a linked worktree to its main checkout | Coordination needs a separate physical checkout identity; do not repurpose history identity |
| `src/daemon/state.py:9-49`, `src/file_lock.py` | Private service state and OS file lock helpers exist | Reuse private-state conventions; do not use tracked Markdown as a concurrent database |
| `src/schedules/heavy.py`, `src/schedules/lease.py` | Autodev already has specialized process/run coordination | Do not replace these locks with generic model messages or claim a reservation controls their processes |

The source tree at the baseline contains no general cooperation journal or agent ownership protocol. The local `codex/shared-kv-mcp` branch at `0268879f` contains a documentation handoff; its diff from its merge base contains documents and a flow, not a shipped KV service. It is neither an existing coordination backend nor a prerequisite for this proposal.

The daemon bearer credential is shared service access, not an authenticated chat identity (`src/daemon/app.py:194-214,311-326`). The request UUID changes per call. Registration therefore needs its own participant capability. Also, `ClientContext.workspace_target` cannot authorize an external worktree just because memory maps it to a main checkout; preserve its containment check and add a dedicated verified checkout path. The new journal must use one installed service authority because the default service-state namespace is installation-path-derived.

CodeGraph was consulted first. TokenSave reported that `dev` was not tracked and served `main`; its output was not accepted as evidence of this baseline. Relevant source was inspected directly at the implementation baseline. No repository indexing was initiated.

## External primary sources

These sources inform specific choices; none establishes that Agents-Core already implements the proposed behavior. Accessed 2026-10-15.

| Source | Supported observation | Design use and boundary |
|---|---|---|
| Reid G. Smith, [The Contract Net Protocol](https://www.reidgsmith.com/The_Contract_Net_Protocol_Dec-1980.pdf), IEEE Transactions on Computers, December 1980 | Nodes exchange task announcements, bids and awards; negotiation uses mutual selection | Use explicit proposals and acknowledgements for targeted overlap resolution; do not reproduce a broadcast auction |
| A2A, [Life of a Task](https://a2a-protocol.org/latest/topics/life-of-a-task/) | Tasks have lifecycle/identity; messages and artifact outputs have different roles; terminal tasks are not restarted | Separate coordination events from immutable results and start a fresh attempt when assignment changes; no A2A wire-compatibility claim |
| Anthropic, [Building effective agents](https://www.anthropic.com/engineering/building-effective-agents) | Orchestrator-workers, parallelization and evaluator-optimizer are distinct patterns | Match collaboration topology to task dependencies; avoid a universal many-agent discussion loop |
| etcd, [Notes on locks and leases](https://etcd.io/docs/v3.6/learning/why/#notes-on-the-usage-of-lock-and-lease) | A lease alone cannot prevent a delayed client from writing; the protected resource must validate ownership/version | Journal generations protect journal mutations only. Worktree isolation and a designated integrator address arbitrary file writes |
| Git, [git-worktree](https://git-scm.com/docs/git-worktree) | Linked worktrees separate checkout/index/HEAD while sharing other repository state | Treat checkout files separately from shared refs/configuration and integration targets |
| SQLite, [Transactions](https://www.sqlite.org/lang_transaction.html) | Explicit write transactions serialize writers and can return busy | Use bounded transactional mutations and explicit retryable contention, not read-then-write JSON replacement |

The original FIPA Contract Net page could not be fetched (HTTP 403); it is not relied upon. The Smith paper above was read directly. This is a targeted engineering study, not an exhaustive literature review or empirical comparison of multi-agent frameworks.

## Alternatives and decisions

| Alternative | Benefit | Limitation | Decision |
|---|---|---|---|
| Rules and native messages only | Small change; useful inside one parent tree | No durable cross-session identity, atomic ownership or replay | Ship as conservative fallback, not as the full solution |
| One global parent for all sessions | Simple authority hierarchy | Independent chats have no common parent and cannot be commandeered | Keep hierarchy within each task only |
| Shared Markdown/JSON file committed to Git | Visible and portable | Concurrent writes, branch divergence and stale copies; no atomic negotiation | Reject as authoritative state; export summaries only |
| Existing memory/history as a message bus | Already available | Different lifecycle and privacy semantics; no agreement transaction | Reject; retain evidence references only |
| External Redis/etcd/A2A service | Future federation and richer infrastructure | Adds deployment/authentication and scope beyond one machine | Defer; use their design lessons without requiring them |
| Local SQLite journal with typed operations | Atomic decisions, durable replay, deterministic tests using standard library | One installation/machine, trusted cooperating participants, no shell fencing | Recommended MVP |

The chosen design spends complexity on deterministic state transitions and recovery, not on autonomous bidding or model-selected numeric priority scores. A small on-demand protocol keeps ordinary single-agent turns cheap.

## Principal failure modes

The design must handle concurrent acceptance of two proposals, stale proposals, duplicate delivery, abandoned participants, contradictory priorities, dependency cycles, priority inversion, never-ending negotiation, outdated evidence, duplicate logical tasks, authority spoofing and case/symlink aliases. A high-priority request cannot revoke a running writer merely by announcing urgency. An expired heartbeat cannot prove the writer stopped.

The detailed remedies and acceptance scenarios are in [protocol.md](protocol.md) and [evaluation.md](evaluation.md). Proposed defaults need evaluation; no speedup, token saving or reliability percentage has been measured.
