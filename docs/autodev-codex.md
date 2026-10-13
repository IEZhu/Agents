# Autodev Codex

How the self-development schedule runs Codex, as a reviewer (a worker) or as a stage client, and
when it falls back to the Claude worker (issue #344, epic #336). The design is
[autodev-plan.md](autodev-plan.md), sections 5.6 and 5.7, Q8 and Q14, as amended by #344. The
Claude side is [autodev-sandbox.md](autodev-sandbox.md) (#343), whose helpers this part reuses.

| Code | What |
|---|---|
| `src/schedules/codex_sandbox.py` | The builders: paths, the outer Seatbelt profile, argv, environment, `CODEX_HOME` hygiene, login |
| `src/schedules/codex_proxy.py` | The runner's CONNECT proxy |
| `src/schedules/codex_probes.py` | The model-free probes, the nested-sandbox check, the decision (`decide`), the guarded start (`start`), the stage availability check (`stage_availability`) and the installed-version checks |
| `tests/test_schedules_codex.py`, `tests/fixtures/fake_codex.py` | The tests and the fake `codex` |

## Current limits

- **On this Mac, Codex is never used.** A nested Seatbelt sandbox does not work on macOS 26.5.1
  (see [What the research changed](#what-the-research-changed)), so `decide` returns the Claude
  worker with `nested_sandbox_unavailable` and `stage_availability` stores Codex as unavailable.
  The code still builds, probes and records everything, so that a macOS where the nested check
  passes, or an owner decision on the [open question](#open-decision-for-the-owner), changes the
  outcome without new code paths.
- **Nothing starts a worker yet.** The worker launcher (#345) calls `codex_probes.decide` before
  every Codex worker, records the result with `record_decision`, and starts Codex only through
  `codex_probes.start`; the run sequence (#347) starts a Codex stage session only while
  `read_availability()` says it is available, and the settings page (#348) shows the stored
  reason.
- **The live check is the owner's.** `python -m src.schedules codex-probe --confirm-live-probe`
  runs the real checks. Nothing in the tests starts the real `codex`; the profile tests apply the
  generated profile with the real `/usr/bin/sandbox-exec` to files and listeners they create.

## What the research changed

The research of 2026-10-14 (codex-cli 0.162.1, the source at tag `rust-v0.162.1`, the help text,
and `sandbox-exec` experiments with a fresh `CODEX_HOME` and a fake home) found:

- **Nested Seatbelt is impossible on this macOS.** Inside any outer `sandbox-exec` profile, an
  inner `sandbox_apply` of a different profile fails with `sandbox-exec: sandbox_apply: Operation
  not permitted` (exit 71), even when the outer profile is `(allow default)`. Only re-applying the
  byte-identical profile works. `codex sandbox -- /bin/echo ok` fails the same way under an outer
  profile, so every model command of `codex exec -s read-only` or `-s workspace-write` would fail.
  The plan's rule applies: without a working nested sandbox, the reviewer is Claude.
- **`-s read-only` reads the whole disk.** Codex compiles it to `(allow file-read*)`: `~/.ssh`,
  the keychain files and `CODEX_HOME/auth.json` are readable by model commands. The outer profile
  is the only read barrier. `workspace-write` reads everything too; it blocks the network by
  default.
- **`--ignore-user-config` drops only `config.toml`.** The user layer stays, so `hooks.json`, the
  `rules/` (unless `--ignore-rules`), skills and `AGENTS.md` of `CODEX_HOME` still load. Hence the
  [hygiene check](#codex_home-and-login) and the `--disable` switches.
- **Skills and plugin marketplaces are also read from `$HOME`** (`~/.agents/skills`,
  `~/.agents/plugins/...`), whatever `CODEX_HOME` says. `HOME` is therefore an empty per-run
  directory, and the outer profile denies the real home.
- **`codex exec` reads `CODEX_API_KEY`, not `OPENAI_API_KEY`**, and `shell_environment_policy`
  defaults to inheriting everything, the key included, into model commands. The argv sets
  `shell_environment_policy.inherit=core` (in the source: `PATH`, `SHELL`, `TMPDIR`, `TEMP`,
  `TMP`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `LOGNAME`, `USER`), so neither the key nor the
  proxy variables reach a model command.
- **`workspace-write` keeps `<root>/.git` read-only**, so a Codex stage session could not commit
  even with a working nested sandbox.
- **Seatbelt network rules accept only `*` or `localhost` hosts** (`host must be * or localhost
  in network address`), so allowing only `api.openai.com` needs the runner's
  [proxy](#the-proxy); codex honours `HTTPS_PROXY` (its `doctor` sent `CONNECT api.openai.com:443`
  to a local logger).
- **An empty `CODEX_HOME` falls back to `~/.codex`**; the environment check refuses one.
- `model_reasoning_effort` accepts any non-empty string; the known values are `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, `max`, `ultra` and `persistent` (for `gpt-6.1-sol`, `ultra`
  is sent as `xhigh`).

## The outer profile

`codex_sandbox.profile_text(kind, names)` builds a deny-default SBPL profile from the research's
tested `outer-final.sb`. **No path of the run is in the text**: every path reaches it as a
`-D NAME=<resolved path>` parameter of `/usr/bin/sandbox-exec` (`codex_sandbox.parameters`), so a
path with `")(allow default)(` in it is only data; the text depends only on the kind and on how
many read roots and ancestors there are. `write_profile` writes it 0600 into a runner-owned
directory and reads it back; `check_launch` compares the file on disk again before a start.

| Rule | What |
|---|---|
| `process-exec` | The codex package (`CODEX_PKG`), `/bin`, `/usr/bin`, `/usr/libexec`, the Command Line Tools |
| `sysctl-read`, `file-read-metadata`, `signal` (same sandbox) | What codex needs to start (without `sysctl-read` it aborts) |
| `file-read*` | `/`, `/System`, `/usr/lib`, `/usr/share`, `/Library/Preferences`, `/bin`, `/usr/bin`, `/usr/libexec`, `/private/etc/ssl`, `/private/etc/hosts`, the Command Line Tools, `xcode_select_link`, the package, `/dev/null`, `/dev/urandom`, `/dev/random` |
| `(deny file-read* (subpath HOME_DIR))` | The owner's home directory, then re-allowed below: |
| `file-read-metadata` (literal) | `HOME_DIR` and every directory between it and a run directory inside it (`ANCESTOR_<n>`); without them codex fails to canonicalise `CODEX_HOME` |
| `file-read*` | `CODEX_HOME`, `TMP_DIR`, `OUT_DIR`; a worker adds `TREE`, `MATERIALS` and the `SCHEMA` file; a stage adds `REPO`, `EXCHANGE`; `READ_ROOT_<n>` |
| `file-write*` | `CODEX_HOME`, `TMP_DIR`, `OUT_DIR`, `/dev/null`, `/dev/dtracehelper`; a stage adds `REPO` and `EXCHANGE` but denies `REPO_GIT_CONFIG` and `REPO_GIT_HOOKS` |
| `network-outbound` | Only `localhost:<PROXY_PORT>`; DNS is denied with everything else |

`CODEX_PKG` is the directory around the resolved executable (`<package>/bin/codex` with
`codex-package.json`, here `/opt/homebrew/Caskroom/codex/0.162.1`); a broad system directory, a
package in the home directory or one overlapping a run directory is refused. The argv names the
resolved executable, and the launch binds its path and SHA-256 (`sandbox.executable_identity`),
so an upgrade between the probes and the start is refused. `CodexPaths.problems` refuses a run
directory that equals or contains the home directory, `~/.ssh`, `~/Library` or the keychains, a
read-only directory inside a writable one, nested writable directories, a profile file the
process could reach, and a stage's one-off repository whose `.git` is not a directory (a gitdir
file would move the `.git/config` and hooks the profile denies).

Checked on 2026-10-14 with the real codex 0.162.1, a fresh `CODEX_HOME` four levels deep in a fake
home and local listeners: `codex --version` runs (no canonicalisation warning), `codex sandbox --
/usr/bin/true` exits 71 with `sandbox_apply: Operation not permitted`, reads and listings of the
fake home, `.ssh` and the state directory are denied, the tree and the materials (in a directory
named `mat")(allow default)(x`) are readable but not writable, the schema is readable, the output
directory and `$TMPDIR` are writable, and only the proxy port is reachable (another local port and a direct
connection to an external address are refused). Codex's `tmp/arg0` links point at the resolved
executable.

## argv

`codex_sandbox.argv` builds `/usr/bin/sandbox-exec -f <profile> -D NAME=value ...` and then:

```text
worker: <codex> exec --ignore-user-config --ignore-rules --ephemeral -s read-only --skip-git-repo-check
          -C <tree> --output-schema <schema> -o <out>/last-message.txt <hardening> [-m M] [-c model_reasoning_effort=E] -
stage:  <codex> exec --ignore-user-config --ignore-rules --ephemeral -s workspace-write --skip-git-repo-check
          -C <one-off repository> <hardening> -c sandbox_workspace_write.exclude_slash_tmp=true --json
          -o <out>/last-message.txt [-m M] [-c model_reasoning_effort=E] -
```

The prompt goes on standard input (`-`). `check_argv` refuses `--add-dir`, any `--dangerously-*`
flag and `danger-full-access` anywhere, and an argv that does not start with the profile prefix.

The hardening switches, each in `codex exec --help` or `codex features list` of 0.162.1, with its
config key read in the source at the tag:

| Switch | Closes |
|---|---|
| `--disable hooks`, `--disable plugins`, `--disable remote_plugin`, `--disable apps` | Hooks (including `CODEX_HOME/hooks.json`), local and remote plugins, the Apps connectors |
| `-c project_doc_max_bytes=0`, `-c project_root_markers=[]` | Project `AGENTS.md`; the walk above `-C` for project configuration |
| `-c analytics.enabled=false`, `-c web_search="disabled"` | Analytics; web search |
| `-c skills.bundled.enabled=false`, `-c skills.include_instructions=false` | Bundled skills; the skills instructions block |
| `-c shell_environment_policy.inherit=core` | Secrets in model commands (see above) |

## Environment

`codex_sandbox.environment` is an allowlist, separate from `runner.run_environment` (which copies
the owner's `HOME` and `TMPDIR`): `PATH`, `HOME` (an empty 0700 directory inside the run's
temporary root, `prepare_run_home`), `LANG`, `CODEX_HOME`, `TMPDIR` (the run's temporary root
`/private/tmp/ad-<8 hex>`, `sandbox.make_temp_root`), `HTTPS_PROXY` and `HTTP_PROXY`
(`http://127.0.0.1:<proxy port>`) and, only in the keychain login mode, `CODEX_API_KEY`.
`check_environment` refuses any other name, a name with `TOKEN`, `KEY`, `SECRET` or `PASSWORD`
other than `CODEX_API_KEY`, an empty `CODEX_HOME`, and any value the outgoing-text scan flags.

## The proxy

`codex_proxy.ConnectProxy` listens on 127.0.0.1 on an ephemeral port, started before the profile
is written. It tunnels only `CONNECT <host>:443` for the login mode's hosts
(`codex_sandbox.allowed_hosts`: `api.openai.com` for an API key, plus `chatgpt.com` and
`auth.openai.com` for a ChatGPT login), relays the bytes without reading them (no TLS
termination), answers `403` to every other target and to any request that is not `CONNECT`, and
records each requested `host:port` (at most 500) and the connections it accepted. Closing it ends
every tunnel; the run uses it as a context manager. The tests use only local listeners (an
injected `connect`).

## CODEX_HOME and login

`CODEX_HOME` is `<state>/schedules/codex-home` (0700), persistent and owned by the runner, logged
in once by the owner to a **separate account with a spending limit** (Q8), never the owner's
interactive `~/.codex`:

```text
CODEX_HOME=<state>/schedules/codex-home codex login             # ChatGPT, in the browser
printenv KEY | CODEX_HOME=<state>/schedules/codex-home codex login --with-api-key
```

A model command can write `CODEX_HOME`, so before every start `hygiene_problems` refuses
(`codex_home_unclean`) `AGENTS.md`, `AGENTS.override.md`, `hooks.json`, `config.toml`, any
`*.config.toml` profile, `rules/`, `plugins/` and any entry of `skills/` other than codex's own
`.system` cache, which must be a plain directory (names compare case-insensitively). The login check runs `codex login status`
with that `CODEX_HOME` and an empty `HOME` (no model turn; in the source it exits 0 when a login
is stored and 1 otherwise); its output names the login and part of an API key, so it goes to
`/dev/null` and only the exit status and a fixed message are kept (`login_missing` names the
command above). `autodev.json` `codex.login` chooses the mode: `api-key` (default) or `chatgpt`
(the stored login; it selects the proxy's hosts) or `keychain:<service>`, which reads an API key
from that keychain item, keeps it in memory and passes it as `CODEX_API_KEY` (no `login status`).

A residual risk stays: the outer profile must let the codex process read `CODEX_HOME`, and its
model commands run under the same profile, so a model command could read a stored `auth.json`. In
the keychain mode the key is only in the codex process's environment, which model commands do not
inherit. That is why the account is separate and limited.

## Probes and the decision

`codex_probes.decide(codex, paths, ...)` returns a `Decision`: Codex with its launch, or the
Claude worker with a machine-readable reason. In order:

| Check | Reason when it fails |
|---|---|
| macOS | `platform_unsupported` |
| The executable (tools.json, then `PATH`) | `codex_missing` |
| `codex --version` is one of `VERIFIED_VERSIONS` (0.162.1) | `codex_version_unverified` |
| `CODEX_HOME` hygiene | `codex_home_unclean` |
| Login (`codex login status`, or the keychain item) | `login_missing` |
| The launch builds and passes `check_launch` | `profile_broken` |
| Model-free probes (below) | `deny_probe_succeeded`, `profile_broken` (codex does not start under the profile), `probe_failed` |
| The nested-sandbox check | `nested_sandbox_unavailable` |

**Model-free probes** run with exactly the launch's profile prefix, environment and working
directory; only the command differs (`CodexLaunch.wrap`):

| Probe | Expected |
|---|---|
| `control.codex-start`: `codex --version` | runs (else the profile is broken) |
| `control.read-materials` (a runner-written seed) or `control.read-exchange`; `control.list-tree` | allowed |
| `control.write-output`, `control.write-tmpdir`; a stage: `control.write-repository`, `control.write-exchange` | allowed; the runner checks the files |
| `control.proxy-port`: `nc -z 127.0.0.1 <port>` | allowed; the proxy counts the connection |
| `read.<decoy>` for every file decoy (#343) and the ephemeral decoy in the owner's temporary directory | denied; the output is scanned and a marker trips the schedule |
| `list.ssh-dir`, `list.keychains-dir`, `list.secrets-dir` (`~/.config/agents-autodev`), `list.home`, `list.state-dir` (each when present) | denied; output discarded |
| `write.decoy-dir`, `write.private-dir` (a runner directory outside the run); a worker: `write.tree`, `write.materials`; a stage: `write.repo-git-config` (append), `write.repo-git-hooks` | denied; the file stays absent or unchanged |
| `network.direct`: `nc` to the external host's address, resolved by the runner | denied (a runner that cannot resolve it fails the probes) |
| `network.proxy-refused`: `curl -x <proxy> https://example.com/` | denied, and the proxy's log shows `example.com:443` refused |

Exit codes 126 and 127, a timeout or a missing proxy record are `inconclusive`, a failure. The
**nested check** runs `codex sandbox -- /usr/bin/true` under the outer profile: exit 0 means the
inner sandbox applied; anything else (here exit 71) is `nested_sandbox_unavailable`. Detection is
the runner's (exit codes, files, the proxy's log); captures are bounded and scanned, and only
verdicts, exit codes and hit metadata reach the record. Any deny-probe success aborts the Codex
step (`deny_probe_succeeded`); a decoy read also turns the schedule off (`decoys.trip`).

`record_decision(run_id, directory, step, decision)` appends to the run record's `clients` list:
`step`, `client_requested`, `client_applied`, `fallback_reason`, `detail`, the launch's digest and
flags, and the probe report, masked with the decoy markers. `start(decision, prompt, proxy=...)`
starts Codex only for a Codex decision whose probes passed at most 15 minutes ago for the same
digest (argv, environment, working directory, profile, executable), with its proxy running and
`check_launch` passing again; it is the only function that starts codex for a model turn.

## Codex as a stage client (Q14)

`stage_availability` decides Q14 and stores the result in `<state>/schedules/codex-availability.json`
(`read_availability` returns `available: false` with `not_checked` until a check ran). It needs:

1. the stage launch passing `decide` (model-free probes and the nested check);
2. the installed `workspace-write` blocking the network (`installed_checks`, below);
3. then, and only then, **the #343 probe plan through `codex exec`** with exactly the stage argv and
   environment (a model turn): runner-written scripts in the exchange directory (controls, reads of
   the decoys and secret places, the keychain items that exist and the decoy item, the network,
   writes outside the run), each run as `/bin/bash <script>`, their `__probe__ <id> rc=<code>`
   lines read from the `--json` events (`item.completed` with `command_execution`; the event
   format is from `exec/src/exec_events.rs` at the tag). A command that is not a planned script
   (after unwrapping `bash -lc '...'`, whose exact form in the events is not verified), any other
   item kind (a file change, a web search, an MCP call), a script that did not run or changed, a
   missing keychain probe (`C3 untested`) or a scan hit fails the check.

Codex stays unavailable with the reason (`stage_probe_failed`, `deny_probe_succeeded`,
`workspace_write_network_open` or a `decide` reason) unless every probe is denied and every
control passed. On this Mac step 1 already fails, so no model turn is spent.

## Installed-version checks

`installed_checks(codex, path=...)` runs model-free and unnested, with a fresh `CODEX_HOME` and
`HOME`, a runner-written file and a local listener only: the version (`codex --version`), the
known effort values, whether `codex sandbox` (read-only by default) reads a file outside its
working directory (`read_only_restricts_reads`; the research: it does not restrict), and whether
`codex sandbox -c sandbox_mode="workspace-write" -- nc ...` reaches the listener
(`workspace_write_blocks_network`; the research: it is blocked). Each result is `null` when its
controls fail (`codex sandbox -- /bin/echo ok` must run; a plain `nc` must reach the listener).
`autodev.json` refuses a Codex profile whose `effort` or `effort_followup` is not a known value.

## Command

```text
python -m src.schedules codex-probe [--kind worker|stage] --confirm-live-probe
```

It refuses without the flag. `worker` builds a launch with a synthetic tree, materials and schema
in the run directory and runs `decide` plus `installed_checks`; `stage` uses a one-off repository
at `pinned` (like `probe`) and runs `stage_availability`. The result is a run record with
`client_requested`, `client_applied`, `fallback_reason` and the report; the exit code is 0 only
when Codex is available.

## Open decision for the owner

**Not implemented:** running Codex with `-s danger-full-access` (or
`--dangerously-bypass-approvals-and-sandbox`) inside the outer profile. The research showed that
it works there (`codex sandbox -c sandbox_mode="danger-full-access" -- /bin/echo ok` runs, and the
outer profile still denies the home reads, the writes and the ports), and the flag's help says it
is "intended solely for running in environments that are externally sandboxed". The outer profile
would then carry all confinement. Residual risks, if the owner chooses it:

- one layer instead of two: a hole in the outer profile or in Seatbelt is a hole, with no inner
  sandbox behind it;
- model commands share the codex process's profile, so they could reach the proxy port and use
  the allowed hosts (with no key in their environment, but `CODEX_HOME/auth.json` is readable);
- the read-only versus workspace-write distinction moves into the outer profile (a worker's tree
  read-only, a stage's repository writable, `.git/config` and hooks denied), so a profile bug
  changes what Codex may write;
- mach services: the deny-default profile denies them all, and a real TLS session may need
  `trustd` or `SecurityServer`; allowing `SecurityServer` would reopen the keychain;
- the plan's rule "a variant without the outer profile is forbidden" still holds; this variant
  keeps the profile but drops Codex's own sandbox, which the plan did not foresee.

The code refuses the flag (`check_argv`); adopting it needs a reviewed change and new probes.

## Open items for the live check

- **TLS under the profile.** No real TLS session ran under the deny-default profile: every
  `CONNECT` in the research got a local 403. Codex imports `SecTrust*` and
  `SCDynamicStoreCopyProxies`; a live `codex exec` may need `com.apple.trustd.agent` or
  `SystemConfiguration.configd`. Only relevant once the nested check passes somewhere.
- **The hosts a real `codex exec` needs** with an API key (`api.openai.com` is likely) and with a
  ChatGPT login (`chatgpt.com`, `auth.openai.com`) are from the source, not observed.
- **The `skills/.system` cache.** Whether `skills.bundled.enabled=false` also stops codex from
  loading content planted under `CODEX_HOME/skills/.system` is not verified; the hygiene check
  allows that name only because codex manages it.
- **Event commands.** How `codex exec --json` reports a command (`bash -lc '...'` or the plain
  string) is not verified; an unrecognised form fails closed.
- **Paths deeper in the real home.** The ancestor metadata rules were tested four levels below a
  fake home, not against the real `/Users/<owner>`.
- **`installed_checks` with the real codex** ran only with the fake in the tests; the expected
  values come from the research's runs.

## Files

| Path | What |
|---|---|
| `schedules/codex-home/` | The runner's `CODEX_HOME` (0700), logged in by the owner |
| `schedules/codex-availability.json` | Whether Codex is available as a stage client: `available`, `reason`, `checked`, `version`, `digest`, the installed checks |
| `schedules/runs/<run-id>/codex-profile/codex.sb` | A check's outer profile (0600) |
| `schedules/runs/<run-id>/codex-out/` | A check's output directory |
