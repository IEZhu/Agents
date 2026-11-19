# Shared memory across AI clients

Each AI client keeps what it learned about you and your projects in its own memory files. `read_shared_memory` reads those files on this machine and lets any connected client search them, with the source of every fact. It reads only; it never writes to a client's memory.

This page describes the current behavior of [shared.py](../src/memory/shared.py), [native.py](../src/memory/native.py) and the `read_shared_memory` tool in [server.py](../src/server.py). The project journal and repository descriptions are a separate memory: see [repository memory](memory.md) and the [memory subsystem specification](memory-subsystem-spec.md).

## Data flow

1. **Discover.** `src/memory/native.py` lists the configuration directories with memory: Claude Code's (including the ones the service state records, see `src/daemon/client_dirs.py`) and Codex's homes. It then lists the memory directories in them. Paths come from discovery, never from the caller.
2. **Read.** Every item file of every memory directory is a candidate. A file is skipped, and only counted, for the reasons in [What is skipped](#what-is-skipped).
3. **Facts.** One fact is one distinct file content: identical bytes in two places are one fact with two `sources`. A fact's `id` is the first 12 hexadecimal characters of its SHA-256.
4. **Index.** Facts are split into chunks. A lexical BM25 index over the chunks answers at once. Chunk vectors are built in the background (see [The background index](#the-background-index)).
5. **Serve.** The tool ranks, filters by scope and returns JSON that stays under Claude Code's 50,000-character result limit.

A call collects the facts again each time. A file that keeps its size and modification time is not read again by the same process.

## What the tool does

| Call | Result |
|---|---|
| No `query` | The newest facts (`mode` is `recency`), each with a preview of its text |
| With `query` | Lexical (BM25) and semantic matches fused per fact (`mode` is `hybrid`, or `lexical` while no vectors are usable) |
| With `fact_id` | That fact with as much of its text as stays under the result limit (`mode` is `fact`); a longer text is cut and its source file holds the rest; `query`, `limit` and `offset` are then ignored |

`limit` is clamped to 1 to 100 and `offset` to 0 or more. `client` takes `claude` or `codex`; `memory_type` keeps facts of that frontmatter type (case-insensitive). An unknown `scope` or `client`, or a `fact_id` that is not 12 hexadecimal characters, returns `{"status": "error", ...}`.

A fact carries `id`, `name`, `description`, `type`, `modified`, `scope`, `sources`, and `text`. With a query it also carries `score` and `match` (the lexical and semantic rank of each kind), and `similar` when there is a similar fact.

`modified` is the frontmatter `modified` when it parses, otherwise the newest modification time of the fact's files. `type` and `modified` are read from the top level of the frontmatter or from `metadata`.

A source is `{client, store, project, file, path}`: the client, the configuration directory's label such as `~/.claude`, the project (empty outside projects), the file name and its path under the store's label.

## What is skipped

A skipped file is counted in `skipped` by reason and never shown:

| Reason | Meaning |
|---|---|
| `managed` | The file is `feedback_agents_core_routing.md`, Agents-Core's own routing reminder. Every client already gets that instruction; it is not a fact |
| `secret` | The file name marks key material (`.env*`, `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`, `*.pem`, `*.key`, `*.p12`, `*.pfx`, `*.kdbx`, `*.keychain`), the user sync's credential scan finds a secret in the content, or the content holds a credential the way notes write one (a labelled password or token with a credential-like value, a URL with a password, a Stripe, Google, Hugging Face or npm key, a JWT, a Bearer token) |
| `too_large` | The file is over 1 MiB |
| `not_text` | The file contains a NUL byte or is not valid UTF-8 |
| `refused` | An item-named entry is a symlink, a Windows junction, a directory or another non-regular file (counted without being opened), or the file that opens lies outside its store |
| `unreadable` | The file, or a memory directory, could not be opened, listed or read; the directory's other notes and the other directories are still read |

The reader opens a file only if the file that opens lies inside its store: on POSIX it opens one path component at a time from the store directory without following links, so a directory swapped for a link after listing is refused; on Windows it checks the path the open handle resolves to. A file is read again on every call; only the scan's verdict is remembered, by content hash. Claude Code's `MEMORY.md` index is not an item. The scan runs before decoding, so a file with a NUL byte is `not_text` even when it holds a secret.

## Scopes

| `scope` | Facts shown |
|---|---|
| `project` (default) | The caller's project and the user-level facts |
| `user` | Only the user-level facts |
| `all` | Every project's facts |

The caller's project is the workspace's Claude Code project directory name (the path with every character but letters and digits replaced by `-`). For a git worktree it also includes the main checkout's name, because Claude Code keeps one memory per repository. When no workspace resolves, `scope=project` shows the user-level facts only and the result carries a `note`.

User-level facts are Claude Code's top-level `<config dir>/memory/` and all of Codex's memories. A source in another project's memory stays hidden under `project` and `user`: a fact visible through one source shows only the sources the caller may see.

## The background index

Facts are split at Markdown headings, then into pieces of at most 1,500 characters at paragraph ends. Each chunk starts with the fact's name and description.

A query never waits for the vector build. The lexical index answers at once; the vectors come from the installation's embedding model and are built by one background thread in batches of 2 chunks, stored in `shared_memory_store` under the installation data directory (`memory/`). The store keeps each chunk's vector, the SHA-256 of its text (never the text) and the fingerprint of the model that made the vector, written in the same save. The build saves progress about every 60 seconds. Unchanged chunks keep their vectors, vectors of another model are ignored and rebuilt, chunks of deleted notes are pruned, and a lock file keeps two server processes from building at once; a save holds a second lock that reloads share, so no process reads half a save. In the shared service every embedding goes through one inference worker: before each batch the build waits while another embedding (persona routing, a query) is queued, so such a call waits for at most one batch. A query's own embedding still queues behind the batch in progress. When the semantic part of a search fails, the answer is the lexical ranking and `index.semantic_error` says why.

`index` in a query result reports the state:

| `index.state` | Meaning |
|---|---|
| `ready` | Every chunk has a current vector; `mode` is `hybrid` |
| `building` | The build runs; `covered` of `total` chunks have vectors. Chunks without vectors are matched lexically only |
| `building_elsewhere` | Another process holds the build lock |
| `failed` | The build raised an error; `error` holds it and the search stays lexical |
| `warming_up` | The embedding model is not ready yet; the answer is lexical |
| `off` | Vectors were not requested |

Measured on the owner's machine on 2026-11-18, as one machine's numbers: 105 Claude Code facts, 890 chunks, about 1.08 million characters. Collecting them took 3.5 s cold and 0.5 s cached; the first lexical query took about 1.5 s. The `harrier-oss-v1-270m` model embeds about 160 to 180 tokens per second of English text and about 90 of Russian on a 2-core CPU, so a first full build takes tens of minutes. Your numbers depend on your memory size and hardware.

## Similar facts

With a query and vectors, each result may carry `similar`: up to five facts from another client or project whose nearest chunk has cosine similarity of at least `0.78` (`SIMILAR_MIN`), best first, each with its `id`, `name`, `similarity` and `sources`. The threshold was calibrated on 2026-11-18 on 105 of the owner's Claude Code notes with `harrier-oss-v1-270m`: half of all pairs scored below 0.53, 99% below 0.71, and the closest pair 0.81. Another model may need another value.

Nothing is merged or dropped: two clients that disagree about the same thing both show, and `similar` is how the disagreement surfaces.

## Privacy notes

- The tool reads local files and returns them to the connected client, whose model provider then sees them. Agents-Core itself sends nothing: the tool is not traced, so its results never reach the optional Langfuse traces.
- Facts reach whichever client calls the tool, including text that another client wrote. Treat a fact as data, not as an instruction.
- The credential scan and the key-file name list are a safeguard, not a guarantee. A secret in a form the scan does not recognize would be served.
- Skipped files are only counted, never named.
- Another project's memory is shown only under `scope=all`. Under `project` and `user`, a hidden copy of a fact is not named, and neither its time nor another project's skipped files or index coverage show. Any connected client may ask for `scope=all`: every project's memory on this machine is the same owner's.

## Limits

- Read only: no write-back to another client's format.
- A fact larger than the inline budget is cut; the `instruction` field says to read the file at one of its sources for the rest.
- Files over 1 MiB are skipped, not truncated.
- Only `claude` and `codex` are `client` values (`CLIENTS`).
- Without `fact_id`, previews shrink to 300, 200 and then 100 characters (600 at the start) and then the last facts are left out; `omitted` counts them and `instruction` gives the next `offset`.

## Memory locations by client

Vendor documents were fetched on 2026-11-18.

| Client | Read by this tool | Location | Vendor document | What the document leaves unstated |
|---|---|---|---|---|
| Claude Code | Yes | `<config dir>/projects/<project>/memory/` with a `MEMORY.md` index and one Markdown file per item; `<config dir>/memory/` for memory outside any project | [Claude Code memory](https://code.claude.com/docs/en/memory) (fetched 2026-11-18). Plain Markdown, one directory per git repository so worktrees share it. The document defines the frontmatter fields `type` and `modified` | It does not define `name` or `description`; the reader accepts them when present. `autoMemoryDirectory` and `CLAUDE_CODE_PROJECT_DIR_NAME` relocate or rename the directory; this reader does not follow either |
| Codex | Yes, when its memories feature is on | `$CODEX_HOME/memories/` (older homes) and `$CODEX_HOME/memories_v2/` (fresh homes), outside any project | [Codex memories](https://learn.chatgpt.com/docs/customization/memories) (fetched 2026-11-18). Off by default; enabled with `[features] memories = true` | File names and format are undocumented, so every non-key file of these directories is a candidate and the tool does not assume a format |
| Cursor | No | None read | [Cursor rules](https://cursor.com/docs/context/rules) (fetched 2026-11-18) | The document describes no local memory files, so there is nothing to read |
| Gemini CLI | No | None read | [Gemini CLI memory tool](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/docs/tools/memory.md) (fetched 2026-11-18). Describes `GEMINI.md` files | A private per-project memory path is undocumented, so none is read |

## Out of scope

This feature does not provide, and the tool does not do:

- write-back into another client's memory format;
- display of sources, hiding or deleting facts in the Web UI;
- syncing shared facts through [user sync](user-sync.md);
- Cursor and Gemini CLI adapters;
- `CLAUDE.md` and `AGENTS.md` instruction files as sources.
