# Configuration and observability

Source of truth for behavior: `env.example`, `src/engine/config.py`, `src/utils/langfuse_compat.py`.

## Environment Variables

The core router needs no external API key. Configure `.env` using
[env.example](../env.example). Setup creates `.env` from it, so a new `.env` starts
with these values:

```env
# Optional: set both keys to enable Langfuse tracing. Keep comments on their own
# line: older python-dotenv reads "KEY=  # text" as the value "# text".
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
ANTHROPIC_API_KEY=sk-ant-...    # Optional: for document OCR
AGENTS_DEBUG=0                  # Set to 1 for per-call JSON debug logs
# Optional local generation, disabled when unset; absolute owner JSON path
# AGENTS_LOCAL_INFERENCE_CONFIG=/absolute/local-inference.json
```

Both keys set enable tracing, so leave them empty to run without Langfuse. The
placeholders `pk-lf-...` and `sk-lf-...`, which an `.env` copied from an older
`env.example` may still hold, do not count as keys. The optional
[document OCR server](../src/mcp_servers/document_ocr/README.md) needs a real
`ANTHROPIC_API_KEY`.

Embeddings run locally through ONNX Runtime, Hugging Face Tokenizers and Hub; the initial model download
requires network access. The default model is `microsoft/harrier-oss-v1-270m`
(MIT, multilingual, about 1.1 GB to download and 0.85 GB loaded): on the project's
retrieval sets it ranks implants and skills better than the earlier defaults
([results](embedding-models-eval-results.md)). It loads from a plain-file
copy of the onnx-community export, pinned in
[embedding_prompts.py](../src/engine/embedding_prompts.py), which also loads offline
once downloaded. Setup no longer asks: harrier-270m is the one model for every
machine, replacing the earlier Full, Balanced and Light choices. Setup writes it
to `EMBEDDING_MODEL` in `.env`, together with `EMBEDDING_MODEL_GENERATION`. When
`EMBEDDING_MODEL` is unset, for example after `--skip-index`, standalone stdio
uses it as well. All 37 dense models from the FastEmbed 0.8.1 catalog stay selectable,
`intfloat/multilingual-e5-large` included: edit `EMBEDDING_MODEL` and, with
server sessions stopped, run
`.venv/bin/python -m src.reindex`. An `.env` without
`EMBEDDING_MODEL_GENERATION=2`, such as one copied by hand from `env.example`,
needs that line too; otherwise the next start switches it to the default
([model switch](#model-switch-on-update)). The [shared macOS and Windows service](install.md#shared-macos-and-windows-service)
pins its model in its own configuration.

The dense adapter in `src/engine/onnx_embedding.py` retains the catalog, model
files, pooling, dimensions and cache locations from FastEmbed 0.8.1 while using
the maintained inference and tokenizer packages directly. OCR uses Pillow 13 or
newer independently. The explicit embedding contract and ONNX Runtime version
now enter the derived-index fingerprint, so the first start rebuilds vector
indexes once; source history and cached model weights are retained. Changes to
the adapter's numerical conventions must bump its `EMBEDDING_CONTRACT`. Model
licenses remain in the catalog and `THIRD_PARTY_NOTICES`.

Automatic model-load recovery removes only a managed model directory with an
identified UTF-8, JSON/metadata or ONNX protobuf fault. A failed Hub snapshot
also removes its blob targets only inside the same repository and only when a
complete re-scan under Hub's own per-blob publication lock finds no other
snapshot references. Shared blobs, uncertain or aliased cache/lock paths, busy
publishers and exhausted lock-wait budgets preserve the blob files. This
cooperates with upstream Hub publishers; it does not add a lifetime protocol
for unrelated clients that bypass those locks. Temporary resource, provider or
other unclassified initialization failures keep cached snapshots and fallback
sources available for later offline loads. Hub transport failures retain the
bounded source retries and archive fallback. Caller-owned `AGENTS_MODEL_PATH`
directories are never removed or rewritten. Positive sentinel-only tokenizer
limits are accepted and then bounded by the normal input cap. SigLIP's fixed
padding still requires a finite tokenizer context. Unknown tokenizer/schema
errors remain visible rather than being guessed from exception text.

### Model switch on update

Earlier setups pinned the model chosen then (Full e5-large, Balanced multilingual
MiniLM or Light English MiniLM). The first idle start after an update to this
version moves an `.env` from any of these, or any other model, to harrier-270m
once: it rewrites `EMBEDDING_MODEL`, keeps the old value in a comment, and
records `EMBEDDING_MODEL_GENERATION=2`
([model_migration.py](../src/model_migration.py)). The server restarts itself
into the new model, downloads it in the background and re-embeds the skill,
implant and history indexes; the router cache resets. Until the indexes are ready,
tools answer `warming_up`. Rerunning `init_repo.sh` or `init_repo.bat` applies the
same switch and downloads the model during setup. A model set after the switch,
for example e5-large again, stays: the generation marker is already current. An
`EMBEDDING_MODEL` exported in a standalone server's environment overrides `.env` as before.
The shared service switches in its update transaction instead
([daemon guide](shared-mcp-daemon.md#embedding-model)).

| Setting | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `microsoft/harrier-oss-v1-270m` when unset | Standalone embedding model |
| `EMBEDDING_MODEL_GENERATION` | Unset counts as `1`; setup and the model switch write `2` | Model generation the `.env` reached; an older one switches to the default model once ([model switch](#model-switch-on-update)) |
| `EMBEDDING_PROMPTS` | `on` | Model-specific query and passage prompts ([embedding_prompts.py](../src/engine/embedding_prompts.py)); `off` embeds text as given |
| `EMBEDDING_BATCH_SIZE` | `4` | Documents per embedding batch (1–256) when history, skill or implant indexes are built. Memory grows with batch size, by up to about 68 MB per 512-token document with `intfloat/multilingual-e5-large` (a full harrier-270m index build peaked at about 2.8 GB with the default in the [recorded run](embedding-models-eval-results.md)); larger values may be faster on machines with spare memory. Inputs are also capped at 2048 tokens |
| `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Persistent model cache; the shared service's `install` reads it only from the shell ([daemon guide](shared-mcp-daemon.md)) |
| `AGENTS_CLIENT_REPO_ROOT` | Unset: a verified `workspace` or sole current MCP root; without roots, the nearest `.git`, `CLAUDE.md` or `AGENTS.md` at or above the session directory, else that safe existing directory ([rules](memory.md)) | Explicit stdio memory and workflow target. Refused with `workspace_unsafe` when it is a system, program or home directory. Set it in a per-project MCP entry's `env`, never in the installation `.env`, a shared registration or the Desktop entry |
| `RULES_ENABLED` | `1` | Include [shared rules](../rules/README.md) in loaded context |
| `AGENTS_CLIENT_PROFILE` | `interactive` | `headless` for one-shot runs with no chat UI and no peer sessions whose installation already holds the managed protocol section, such as benchmarks; it trims what activation adds to the context (#734). Every `SUCCESS` but a restore, a first activation included, sends the [section pointer](routing_flow.md#api) instead of the full instruction; the persona bundle is cut to at most the standard tier, the standard tier's two implants and no `agent-cooperation` or `thread-status` rule ([bundle trim](routing_flow.md#enrichment-and-storage)); a [cache miss](routing_flow.md#a-cache-miss) may load a clear embedding leader without a `ROUTE_REQUIRED` round trip (`ROUTER_COLD_RESOLVE_MARGIN`); and with `AGENTS_TURN_LOG_HOOK` the server leaves logging to the hook. Per server process: set it in a standalone stdio entry's `env`, not in a shared daemon that also serves interactive sessions |
| `AGENTS_TURN_LOG_HOOK` | `0` | `1` together with `AGENTS_CLIENT_PROFILE=headless`: the server tells the model not to call `log_interaction`, and the Claude Code Stop hook `python -m src.memory.turn_log_hook` logs each turn instead (#734, [setup](memory.md#turn-logging-by-a-stop-hook)). Either switch alone changes nothing. The hook reads both switches from its own environment, so set them in the environment that starts the headless run. Interactive Claude Code sessions need neither: the hook registered as `python -m src.memory.turn_log_hook --interactive` for both `UserPromptSubmit` and `Stop` logs the turns of each session that runs it and tells the model so with a notice beside every prompt, while a session that runs no hook, and every other client, keeps `log_interaction`; the server reads no settings file for this (#701, [interactive setup](memory.md#turn-logging-by-a-stop-hook)) |
| `ROUTER_SHORTLIST_SIZE` | `8` | How many agents a `ROUTE_REQUIRED` on a semantic-cache miss lists (integer, 0 or more), ranked by `SemanticRouter.cold_ranking`, `universal_agent` always among them ([a cache miss](routing_flow.md#a-cache-miss)). `0` lists the whole catalog as before; `1` leaves no place beside `universal_agent` and logs a warning and uses `8`, as does a value that is not an integer or is negative |
| `ROUTER_COLD_RESOLVE_MARGIN` | `0.05` | Headless profile only: a cache miss loads the first agent of the embedding ranking itself, with `alternatives`, when its cosine similarity to the request leads the second's by at least this much (0 to 2); `0` turns it off, and interactive sessions never resolve a miss. The default is provisional: it was measured on one 72-query Russian labelled set (`evals/scripts/measure_cold_ranker.py`). A value outside the range or not a number logs a warning and uses the default |
| `AGENTS_SUBAGENT_TIERING` | `1` | Resolve model tiers when worker preparation supplies `complexity`; `0` keeps the host model. Per-client model/effort overrides and escalation are in [delegated model tiers](subagent-model-tiers.md#configuration) |
| `AGENTS_COOPERATION_ENABLED` | `0` | Opt the installed daemon into the local [agent cooperation service and mailbox](agent-cooperation/README.md); HTTP and existing stdio-bridge clients share it; standalone stdio returns `store_unavailable`; accepting the setup option or `--yes` sets this to `1` |
| `AGENTS_COOPERATION_RETENTION_DAYS` | `30` | Owner retention window for terminal cooperation receipts/events (integer, minimum 30); invalid values leave coordination unavailable; see [maintenance and recovery](agent-cooperation/runtime.md) |
| `AGENTS_USER_RULES_DIR` | `data/user-rules` in the installation | Machine-local cache for [URL-subscribed user rules](user-rules.md), separate from flow sync |
| `AGENTS_RULE_AUTH_HOSTS` | Unset | JSON map from a Bearer-token environment variable name to its exact permitted source hostname, required when `auth_env` is used for a user rule |
| `AGENTS_RULE_PRIVATE_HOSTS` | Unset | Exact hostnames allowed to resolve to RFC1918 IPv4 for user-rule fetches; comma or whitespace separated; loopback/link-local remain denied |
| `AGENTS_LOCAL_INFERENCE_CONFIG` | Unset; generation disabled | Absolute owner JSON path for optional [local inference](shared-mcp-daemon.md#local-inference) through the shared HTTP daemon. Models and optional dependencies require separate setup; restart after configuration changes |
| `INTENT_CLASSIFIER_ENABLED` | `0` | Enable the optional intent-based enrichment classifier |
| `AGENTS_USER_FLOWS_DIR` | `flows/.user` in the installation | Location (absolute path) of [personal and repository flows](../flows/README.md#personal-and-repository-flows), their persona choices, and the installation's rule, skill and implant switches (`components.json`) |
| `AGENTS_GITHUB_HOST` | `github.com` | GitHub host that [library sync](user-sync.md#github) signs in to and calls; set it for GitHub Enterprise Server |
| `AGENTS_GITHUB_CLIENT_ID` | The Agents-Core OAuth App | Client ID of an OAuth App with Device Flow enabled for [library sync](user-sync.md#github) sign-in; set it for a fork or GitHub Enterprise Server |
| `WARMUP_WAIT_SECONDS` | `20` | Seconds (1–600) a retrieval tool waits for background startup (stores, embedding model, rules) before it answers `warming_up`; the MCP handshake never waits ([details](routing_flow.md#startup-and-readiness)) |

Routing thresholds and enrichment settings are defined in
[src/engine/config.py](../src/engine/config.py). Its environment overrides, such as
`ROUTER_SIMILARITY_THRESHOLD`, `SKILLS_RELEVANCE_THRESHOLD`,
`IMPLANTS_RELEVANCE_THRESHOLD`, the
[`IMPLANT_*` settings](../implants/README.md#1-agent-preferences-and-semantic-retrieval) and the
[intent classifier](intent-classifier.md) thresholds in `env.example`, are
tuning and evaluation settings; normally keep their defaults. HTTP memory uses a
registered workspace header instead of `AGENTS_CLIENT_REPO_ROOT`.


## 📊 Observability

Selected routing, loading, retrieval, and memory operations are instrumented with
Langfuse. `log_interaction` waits for the local journal commit and returns its
`history` result. Only Langfuse runs in the background and reports `queued`
(`skipped` while retrieval warms up). A failed history write is reported in
`history` and `history_last_error` on the same call; Langfuse failures go only to
the server log. Set real Langfuse keys
in `.env` to enable it; leave them empty for local-only operation (see
[Environment Variables](#environment-variables)). Set
`LANGFUSE_TRACING_ENABLED=false` when running checks that should not send traces.
