# Agent programming languages: prior work and opportunity

Research date: 2026-10-14. Companion to the [Accord concept](agent-language-concept.md).

This is a bounded survey of primary papers, upstream repositories and official documentation. It identifies authors and teams already working on the area; it is not an exhaustive literature review, a novelty proof or a ranking of production readiness. Documented implementation and experimental results were inspected, not independently built or reproduced. Mutable source pages describe what was accessible on the research date.

## Who is already working on it

| Project and people | Documented focus | Evidence and maturity | Implication for Accord |
|---|---|---|---|
| **ETAS** — Huiri Tan, Yikun Wang, Puyang Zhang, Shangyu Li, Jiasi Shen | Agents, effects, policies, approvals and traces in one language semantics | [Preprint, 2026-07-20](https://arxiv.org/abs/2607.17780); [Rust implementation](https://github.com/etas-project/etas) explicitly describes an early experimental prototype and incomplete areas | Closest broad overlap; consider extending its contracts before creating another language |
| **Turn** — Muyukani Kizito | Typed inference, actors, durable state and opaque identity capabilities | [Preprint, 2026-03-07](https://arxiv.org/abs/2603.08755); [upstream](https://github.com/ekizito96/Turn); [official site](https://www.turn-lang.dev/) advertises stable v1 and a Turn 2 preview | Agent-native syntax, isolation and durability already have a concrete language implementation |
| **LBAC / TypeGuard** — Timothy Zhou, Loris D'Antoni, Nadia Polikarpova | Generated code checked in the same typed environment as scaffolding; provenance and restricted recursive agents | [Preprint, 2026-05-13](https://arxiv.org/abs/2605.12863) describes a Haskell realization and case studies; implementation maturity here is paper-reported | Provenance-aware computation and attenuated subagent capabilities are direct prior work |
| **BAML** — Boundary ML | Typed AI functions, runtime types, typed errors, concurrency, tests/evals and host bridges | [Current upstream](https://github.com/BoundaryML/baml), [site](https://boundaryml.com/) and [versioned CLI reference](https://developer.boundaryml.com/cli/v0.20.1/commands) describe a broader language; [older docs](https://docs.boundaryml.com/home) retain the structured-output DSL framing | Calling BAML only a prompt DSL would understate its current scope; compare concrete features and versions |
| **Jac / Jaseci** — Jaseci Labs team | Python-like language with object-spatial programming and model-backed function implementations through byLLM | [Official language site](https://www.jac-lang.org/), [byLLM reference](https://www.jac-lang.org/reference/plugins/byllm/), [upstream](https://github.com/jaseci-labs/jaseci) provide language/runtime documentation and source | LLM-backed functions and agent construction already exist within a wider language and runtime |
| **LMQL** — Luca Beurer-Kellner, Marc Fischer, Martin Vechev; ETH Zurich SRI Lab | Python-like model programming, constraints and optimized inference | [Paper](https://arxiv.org/abs/2212.06094), first submitted 2022-12-12, revised 2023-05-30; [implementation](https://github.com/eth-sri/lmql) and [project](https://lmql.ai/) | Constraints on model generation are established language work; they do not by themselves establish task acceptance |
| **SGLang** — Lianmin Zheng and coauthors; SGLang project | Frontend language and runtime for structured model programs, generation and parallelism | [Paper, 2023-12-12](https://arxiv.org/abs/2312.07104) and [upstream](https://github.com/sgl-project/sglang) | Execution efficiency is a separate opportunity; Accord should use inference runtimes rather than recreate one |
| **APPL** — appl-team | Python-integrated prompt programming, composition, concurrent model calls and tool calls | [Upstream](https://github.com/appl-team/appl) documents implementation and examples; current maintenance cadence was not established | A small embedded language is a practical adoption path, not a novel idea in itself |
| **Jason / AgentSpeak** — Jomi F. Hübner and Rafael H. Bordini lead Jason | Agent goals, plans and communication in the belief-desire-intention tradition | [Jason interpreter and platform](https://github.com/jason-lang/jason); [communication semantics paper](https://arxiv.org/abs/1111.0041) by Bordini, Álvaro F. Moreira, Renata Vieira and Michael Wooldridge, published in JAIR in 2007 and deposited in 2011 | Agent languages long predate LLMs; avoid “first programming language for agents” positioning |

The following work is equally relevant but is a runtime mechanism, framework or research agenda rather than a directly comparable new language:

| Work and people | Contribution and limits | Why it matters |
|---|---|---|
| **CapLease** — Jinghan Xu, Longze Fan, Zeyuan Wang, Xinjin Li, Hankai Liu | [Preprint, 2026-08-03](https://arxiv.org/abs/2608.01710) describes durable authorization state, canonical action identity and budget-reserving delegation; duplicate external-effect protection requires an idempotent destination | One-shot tokens, action binding and durable authorization cannot be claimed as Accord inventions |
| **LangGraph** — LangChain team | [Persistence](https://docs.langchain.com/oss/python/langgraph/persistence) provides checkpoints and stores; the [upstream functional API guide](https://github.com/langchain-ai/docs/blob/main/src/oss/langgraph/functional-api.mdx) documents replay and idempotency responsibilities | Checkpointing is not enough to resolve uncertain external effects; compare against a properly configured framework |
| **Agents as Software** — Shraddha Barke, Adithya Murali | [Essay submitted 2026-09-26](https://arxiv.org/abs/2609.32198) proposes specifications over traces and state; it is an agenda, not evidence of an implemented new language | Language-level agent reliability is already an explicit research direction |

## The closest overlaps

ETAS is the strongest reason to narrow the proposal. Its design already connects static checks, residual runtime policies, mediated actions and recovery. Its [upstream agent design](https://github.com/etas-project/etas/blob/main/docs/design/03-agents-tools-prompts-memory.md) also specifies message provenance and versioned memory. Those design specifications should not be represented as demonstrated production guarantees, but their existence rules out broad novelty claims about combining these features.

Turn's current website describes at-least-once replay of pending effects. Its paper describes a confidence operator using provider metadata. Neither should be confused with exactly-once outside-world behavior or calibrated evidence of factual truth. The official website and the browser-visible GitHub snapshot differed in recency; use explicit source/version attribution rather than merging them into an assumed release.

BAML's documentation is evolving. The [preserved original README](https://github.com/BoundaryML/baml/blob/canary/README.v0.md) names Boundary ML and 2024 and describes the original DSL. Current upstream materials present a fuller language. Adoption claims for the older DSL do not automatically establish maturity of every newer feature.

LBAC directly addresses data provenance and recursive agents under the same or stricter restrictions. CapLease directly addresses durable consumption of action authority across retry, delegation and recovery. Their implementation descriptions in this survey come from their papers; no independent upstream build was verified here.

## The underexplored hypothesis

The proposed opportunity is to make **acceptance of delegated work** an explicit, portable program value, with dependency semantics that connect the accepted deliverable to subsequent action eligibility. In the consulted materials, that precise cross-runtime boundary was less directly documented than inference, effects, provenance or durable authorization. This observation remains an inference; a focused artifact review could find an equivalent existing mechanism.

| Hypothesis | Concrete experiment | Closest prior work and risk |
|---|---|---|
| Revision-bound acceptance | A host-issued receipt binds contract, input/output digests, validator revision and observed checks; a wrong-artifact receipt cannot satisfy an action precondition | LBAC provenance, ETAS messages/traces, CapLease context/receipts. A faithful receipt can still attest a weak validator |
| Compositional invalidation | Mutating a required dependency invalidates acceptance and dependent action eligibility, including after resume | ETAS versioned memory and policies, CapLease epochs, Turn durability. Hidden dependencies and commit races can break the intended guarantee |
| Portable contract semantics | One contract runs through two adapters with the same outcome/receipt rules and explicit unsupported operations | Existing languages and frameworks already enforce many local boundaries. One unmediated adapter operation undermines the common semantics |

No claim of novelty should be made until those mechanisms are compared with current implementation artifacts. If an ETAS extension, typed host-language library or framework adapter expresses them adequately, prefer that route over a new compiler.

## Existing work in this installation

Read-only checks also looked for duplication in the user's workspace:

- The linked Git worktree inventory contained no separate agent-language worktree before this task created one. Existing subagent-contract and instruction-signing work is related infrastructure, not evidence of another language implementation.
- The Codex app listing returned 62 chats: all pinned chats plus the 50 most recent unpinned chats. It included the current chat, “Изучи языки агентов и предложи новый”, and related chats “Проверить инструкции по ролям субагн” and “Проверка установки клиентских инструкций”. No other language-design chat was identified in that listing. Older and archived chats were not exhaustively searched, and titles/summaries are weaker evidence than reading full work.
- A GitHub query for `"programming language" in:title,body` across all states of `IEZhu/Agents` issues returned no matches. The open-PR inventory contained [#380](https://github.com/IEZhu/Agents/pull/380), [#384](https://github.com/IEZhu/Agents/pull/384) and [#385](https://github.com/IEZhu/Agents/pull/385), concerning plugin distribution, session readiness and release recording, respectively. This narrow query does not prove that no related issue or unpublished branch exists.
- The source snapshot for this proposal is `origin/dev` at `7ed3d75679ddce3a42975d86d1ec963e241a08ea`. The [subagent contract](subagent-contract.md) is implemented structural validation; [instruction signing](instruction-signing-spec.md) remains a separate draft proposal. Neither is treated as an existing Accord runtime.

No maintainers were contacted and no other chat was messaged. Research inspected available public work rather than establishing anyone's private roadmap or current staffing.

## Recommendation

Start with an acceptance-contract IR and a host-language library. Make wrong-revision acceptance, missing checks, delegated overspending, replay and uncertain effects observable counterexamples. Compare with an equally protected SDK and with the closest implemented ETAS subset. The language is worth pursuing only if it makes these contracts clearer or prevents errors with less developer work; otherwise the useful deliverable is a library and adapter specification.
