# Autodev Codex

How the self-development schedule runs Codex, as a reviewer (a worker) or as a stage client, and
when it falls back to the Claude worker (issue #344, epic #336). The design is
[autodev-plan.md](autodev-plan.md), sections 5.6 and 5.7, Q8 and Q14, as amended by #344. The
Claude side is [autodev-sandbox.md](autodev-sandbox.md) (#343), whose helpers this part reuses.

| Code | What |
|---|---|
| `src/schedules/codex_sandbox.py` | The builders: paths, the per-run directories, the outer Seatbelt profile, argv, environment, `CODEX_HOME` hygiene, login |
| `src/schedules/codex_proxy.py` | The runner's CONNECT proxy |
| `src/schedules/codex_probes.py` | The model-free probes, the nested-sandbox check, the decision (`decide`), the guarded start (`start`), the stage availability check (`stage_availability`), the stored availability and the installed-version checks |
| `tests/test_schedules_codex.py`, `tests/fixtures/fake_codex.py` | The tests and the fake `codex` |

## Current limits

- **The recorded Mac falls back to Claude.** In the 2026-10-14 research, a nested Seatbelt
  sandbox did not work on macOS 26.5.1 (see [What the research changed](#what-the-research-changed)), so `decide` returns the Claude
  worker with `nested_sandbox_unavailable` and `stage_availability` stores Codex as unavailable.
  The code still builds, probes and records everything, so that a macOS where the nested check
  passes, or an owner decision on the [open question](#open-decision-for-the-owner), changes the
  outcome without new code paths. Codex never runs without the outer profile.
- **Workers are integrated.** The worker launcher (#345,
  [autodev-workers.md](autodev-workers.md)) calls `codex_probes.decide` before every Codex worker,
  records the result with `record_decision`, starts Codex only through `codex_probes.start` with the
  derived output schema, runs the Claude worker on a fallback (`decision.fallback`) and **fails the
  step on an abort** (`decision.abort`, see [Probes and the decision](#probes-and-the-decision)),
  running neither client. It returns `decision.known` (the API key as a known secret) to its caller,
  and the worker service forwards each launch's known secrets to its `on_known`; the run sequence
  (#347) points that at `broker.Broker.add_known`, since it builds the broker before any worker
  decides.
- **Only the select stage runs as Codex (#347).** The run sequence starts a Codex stage session
  only for `select` and only while `read_availability(version=..., shape=...)` says it is
  available; then it decides (`decide`, recorded as the step `stage-select`), starts the session
  through `start` with the stage's `--output-schema`, reads the `-o` file and validates it as a
  Claude output is validated. Otherwise, and always for `implement` and `submit`, the stage runs
  as a Claude session with `requested_client`, `client` and `fallback_reason` in its
  record: `codex_stage_commit_unsupported` for a stage that commits, since a `workspace-write`
  session cannot write `.git` and the runner-side commit on its behalf is not built (#347 open
  question Q-codex-commit), or the stored or decided reason (on this Mac
  `nested_sandbox_unavailable`). The settings page (#348) shows the stored reason; both read it
  without a key or a run ([the stored availability](#codex-as-a-stage-client-q14)).
  A scheduled real run starts a fallback Claude stage only after that kind's Claude sandbox probe
  records pass in every required mode, even when the stored Codex check passed and only the live
  decision falls back. Otherwise it ends `refused` with the missing probe commands
  ([automatic mode](autodev-runner.md#automatic-mode)).
- **Profiles remain explicit.** Autodev takes stage and delegate models from `autodev.json`;
  generic prepared-worker model tiers do not replace them
  ([worker profiles](autodev-workers.md#profiles-and-model-tiers)). A worker attempt records
  `client_requested` and `client_applied` inside its `client` object; those names differ from
  a stage record's `requested_client` and `client`.
- **The live check is the owner's.** `python -m src.schedules codex-probe --confirm-live-probe`
  runs the real checks. Nothing in the tests starts the real `codex` or a model turn; the profile
  tests apply the generated profile with the real `/usr/bin/sandbox-exec` to files, a shell-script
  `codex` and listeners they create.

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
  the keychain files and anything in `CODEX_HOME` are readable by model commands. The outer
  profile is the only read barrier. `workspace-write` reads everything too; it blocks the network
  by default and keeps `<root>/.git` read-only, so a Codex stage session could not commit even
  with a working nested sandbox.
- **`--ignore-user-config` drops only `config.toml`.** The user layer stays, so `hooks.json`, the
  `rules/` (unless `--ignore-rules`), skills, `AGENTS.md` and `.env` of `CODEX_HOME` still load
  (`.env` before anything else, for every subcommand, `--version` included). Hence the empty
  per-run [`CODEX_HOME`](#per-run-home-codex_home-and-tmpdir), its allowlist and the `--disable`
  switches.
- **Skills and plugin marketplaces are also read from `$HOME`** (`~/.agents/skills`,
  `~/.agents/plugins/...`), whatever `CODEX_HOME` says. `HOME` is therefore an empty per-run
  directory, and the outer profile denies the real home.
- **`codex exec` reads `CODEX_API_KEY`, not `OPENAI_API_KEY`**, and `shell_environment_policy`
  defaults to inheriting everything, the key included, into model commands. The argv sets
  `shell_environment_policy.inherit=core` (in the source: `PATH`, `SHELL`, `TMPDIR`, `TEMP`,
  `TMP`, `HOME`, `LANG`, `LC_ALL`, `LC_CTYPE`, `LOGNAME`, `USER`), so neither the key nor the
  proxy variables are inherited by a model command (the stage probe `observe.env.script` checks
  it). Inheritance is not the only way to see them, though: see [Residual risks](#residual-risks).
- **Seatbelt network rules accept only `*` or `localhost` hosts** (`host must be * or localhost
  in network address`), so allowing only `api.openai.com` needs the runner's
  [proxy](#the-proxy); codex honours `HTTPS_PROXY` (its `doctor` sent `CONNECT api.openai.com:443`
  to a local logger).
- **An empty `CODEX_HOME` falls back to `~/.codex`**; the environment check refuses one.
- `model_reasoning_effort` accepts any non-empty string. The known values, `none`, `minimal`,
  `low`, `medium`, `high`, `xhigh`, `max`, `ultra` and `persistent` (for `gpt-6.1-sol`, `ultra` is
  sent as `xhigh`), come from the source at `rust-v0.162.1`, not from the installed binary:
  `installed_checks` reports that list (`efforts`) as it is, and `autodev.json` refuses others for
  a Codex profile.

## Per-run HOME, CODEX_HOME and TMPDIR

Every Codex process gets three new, empty 0700 directories inside the run's temporary root
(`/private/tmp/ad-<8 hex>`, `sandbox.make_temp_root`), made by
`codex_sandbox.prepare_run_directories` and removed with the root after the run
(`sandbox.remove_temp_root`, which first gives every directory mode 0700 and clears the user
immutable and append-only flags, never through a link, because a model command may write the
root and `chmod 000` a directory in it): `home` (its `HOME`), `codex-home` (its `CODEX_HOME`) and
`tmp` (its `TMPDIR`; not the root itself, because codex refuses to create its `tmp/arg0` helpers when
`CODEX_HOME` lies under `TMPDIR`). Nothing of a Codex process survives the run, and the worker
and the stage never share one. `CodexPaths.problems` refuses a `HOME` or `CODEX_HOME` that is not
directly inside the temporary root and three directories that are not distinct.

**The `CODEX_HOME` allowlist.** A model command may write `CODEX_HOME`, and codex trusts what it
finds there at its next start, so `hygiene_problems` checks it before the probes and again right
before `start` (`codex_home_unclean` before the decision; `start` refuses afterwards). Only what
codex itself creates may be there: the directories `tmp` and `log` and the files
`state_<n>.sqlite` and `logs_<n>.sqlite` (with `-wal`, `-shm` or `-journal`), plain, not links.
Everything else is refused; `.env`, `AGENTS.md`, `AGENTS.override.md`, `hooks.json`, `config.toml`,
any `*.config.toml`, `rules`, `plugins`, `skills`, `models_cache.json` (cached base instructions)
and `auth.json` (a stored login) are named as sources codex would load (names compare
case-insensitively). `codex --version` in `preflight` and `installed_checks` runs with a separate
scratch `CODEX_HOME` and `HOME`, removed afterwards, never the run's.

## Login: the keychain only

`autodev.json` `codex.login` is `"keychain:<service>"` or `null` (the default). With `null` the
decision is the Claude worker with `login_missing`; the earlier modes `api-key` and `chatgpt` are
refused by the configuration check, which names the keychain mode. The runner reads the item with
`security find-generic-password -s <service> -w` (in memory only; a value that is not printable or
is shorter than 8 characters is `login_missing`) and passes it as `CODEX_API_KEY` to the codex
process only. There is no `codex login`, no `auth.json` and no `login status`.

The owner stores an API key of a **separate OpenAI project with a spending limit** (Q8), never the
key of their own account:

```text
security add-generic-password -a autodev -s autodev-codex-api-key -U -w
```

With `-w` as the last option, `security` prompts for the key, so it never appears on a command
line or in the shell history. Then set `"codex": {"login": "keychain:autodev-codex-api-key"}` in
`autodev.json`. The spending limit is the project's budget in the OpenAI platform settings; the
runner cannot check it, so it is the owner's part of the setup and the main bound on the
[residual risks](#residual-risks).

## The outer profile

`codex_sandbox.profile_text(kind, names)` builds a deny-default SBPL profile from the research's
tested `outer-final.sb`. **No path of the run is in the text**: every path reaches it as a
`-D NAME=<resolved path>` parameter of `/usr/bin/sandbox-exec` (`codex_sandbox.parameters`), so a
path with `")(allow default)(` in it is only data; the text depends only on the kind and on how
many read roots, interpreter roots and ancestors there are. `write_profile` writes it 0600 into a
runner-owned directory and reads it back; `check_launch` compares the file on disk again before a
start. A later rule wins, so the order matters.

| Rule | What |
|---|---|
| `process-exec` | The codex package (`CODEX_PKG`), `/bin`, `/usr/bin`, `/usr/libexec`, the Command Line Tools' `git`; a stage adds its environment (`INTERPRETER_<n>`, below) |
| `(deny process-exec ...)` | Interpreters in those directories that can call `sysctl` or a C function directly: `perl`, `ruby`, `irb`, `erb`, `osascript`, `osacompile`, `swift`, `lldb`, `python*`, `pydoc`, `tclsh`, `wish`, `jrunscript`, `jshell` |
| `sysctl-read` | Only Codex's own name allowlist (`codex-rs/sandboxing/src/seatbelt_base_policy.sbpl` at the tag) plus `kern.bootargs`, `kern.iossupportversion` and `security.mac.lockdown_mode_state`, which a trace of `codex --version` and `codex sandbox -- /usr/bin/true` under the profile showed every process reading at start |
| `signal` | Same sandbox only |
| `file-read-metadata` | Everywhere (see [Residual risks](#residual-risks)); the home directory's is denied again below |
| `file-read*` | `/`, `/System`, `/usr/lib`, `/usr/share`, `/bin`, `/usr/bin`, `/usr/libexec`, `/private/etc/ssl`, `/private/etc/hosts`, the Command Line Tools, `xcode_select_link`, the package, `/dev/null`, `/dev/urandom`, `/dev/random` (no `/Library/Preferences`) |
| `(deny file-read* (subpath HOME_DIR))` | The owner's home directory, then re-allowed below: |
| `file-read-metadata` (literal) | `HOME_DIR` and every directory between it and a run directory inside it (`ANCESTOR_<n>`); without them codex fails to canonicalise its directories |
| `file-read*` | `TMP_DIR` (which holds `HOME`, `CODEX_HOME` and `TMPDIR`), `OUT_DIR`; a worker adds `TREE`, `MATERIALS` and the `SCHEMA` file; a stage adds `REPO`, `EXCHANGE` and `INTERPRETER_<n>`; `READ_ROOT_<n>` |
| `file-write*` | `TMP_DIR`, `OUT_DIR`, `/dev/null`, `/dev/dtracehelper`; a stage adds `REPO` and `EXCHANGE`. Never a read root or an interpreter root |
| Stage: `(deny file-write* ...)` | The `.git` protections, below |
| `network-outbound` | Only `localhost:<PROXY_PORT>`; DNS, other local ports, Unix sockets and every mach service are denied with everything else |

`CODEX_PKG` is the directory around the resolved executable (`<package>/bin/codex` with
`codex-package.json`, here `/opt/homebrew/Caskroom/codex/0.162.1`); a broad system directory, a
package in the home directory or one overlapping a run directory is refused. The argv names the
resolved executable, and the launch binds its path and SHA-256 (`sandbox.executable_identity`),
so an upgrade between the probes and the start is refused. `CodexPaths.problems` refuses a run
directory that equals or contains the home directory, `~/.ssh`, `~/Library`, the keychains,
`~/.config/agents-autodev`, the autodev state directory or the installation checkout; a run
directory that lies inside `~/.ssh`, `~/Library`, `~/.config/agents-autodev` or the state
directory, except the run's own output directory inside the state directory (which lives in
`~/Library/Application Support` on macOS), since the profile re-allows a run directory after the
home deny; a read-only directory inside a writable one, nested writable directories, a profile
file the process could reach, an interpreter root that is too broad (`/usr`, `/nix/store`, ...),
and a stage without a `venv` or whose one-off repository's `.git` is not a directory. So a
worker's tree, materials and schema lie outside the state directory (the `codex-probe` check
writes its synthetic ones to `/private/tmp/autodev-codex-work-*`, and the worker launcher of #345
copies the real ones into a fresh temporary root for every attempt).

**The stage's `.git` protections.** Seatbelt checks a rename only against its source and
destination, so a stage that may rename `.git` could edit its config and hooks under another name
and rename it back (the round-1 review did exactly that). The stage profile therefore denies
writes to the repository and `.git` entries themselves (`REPO`, `REPO_GIT`), to `.git/config`, to
the files that redirect git elsewhere and their directories (`.git/commondir`, `.git/info`,
`.git/info/alternates`, `.git/objects`, `.git/objects/info`, `.git/objects/info/alternates`) and
to everything under `.git/hooks`; writes inside `.git/objects/<xx>/` still work. The probe
`write.repo-git-rename` runs the rename-and-edit sequence and requires config and hooks unchanged
(cleanup restores `.git` if it ever got through).

**The stage's environment** (`CodexPaths.venv`) is readable and executable for the stage, with
the installation prefix of its base interpreter (`sandbox.interpreter_roots`), and never
writable. The control probe `control.venv-python` runs `<venv>/bin/python -c 0` under the profile;
when it fails the stage is unavailable with `venv_unusable`. **A nix-built venv cannot run under
these rules**: its interpreter wrapper executes further store paths the profile does not allow,
and the whole store is too broad to allow. The installation's venv on this Mac is nix-built, so a
Codex stage would be `venv_unusable` here even if the nested check passed.

Checked on 2026-10-14 with the real codex 0.162.1, a fresh `CODEX_HOME` four levels deep in a fake
home and local listeners: `codex --version` runs (no canonicalisation warning) under the sysctl
allowlist without `/Library/Preferences`, `codex sandbox -- /usr/bin/true` exits 71 with
`sandbox_apply: Operation not permitted`, reads and listings of the fake home, `.ssh` and the
state directory are denied, the tree and the materials (in a directory named
`mat")(allow default)(x`) are readable but not writable, the output directory and `$TMPDIR` are
writable, and only the proxy port is reachable. Under the generated profiles a read root is
readable and not writable, a stage's venv is not writable, a hard link, a read through a symbolic
link and a write through a directory link to the fake home are denied, `launchctl submit` exits 1
without a job, `open` exits 1, `nc -U` to a runner socket and to another local port exit 1, the
system resolver finds nothing, `dig` cannot bind a UDP socket (exit 10), and starting `osascript`
or a program outside the executable directories gives bash's 126.

## argv

`codex_sandbox.prefix` builds `/usr/bin/sandbox-exec -f <profile> -D NAME=value ...` and
`codex_sandbox.codex_command` the rest (`build` joins them):

```text
worker: <codex> exec --ignore-user-config --ignore-rules --ephemeral -s read-only --skip-git-repo-check
          -C <tree> --output-schema <schema> -o <out>/last-message.txt <hardening> [-m M] [-c model_reasoning_effort=E] -
stage:  <codex> exec --ignore-user-config --ignore-rules --ephemeral -s workspace-write --skip-git-repo-check
          -C <one-off repository> <hardening> -c sandbox_workspace_write.exclude_slash_tmp=true --json
          -o <out>/last-message.txt [-m M] [-c model_reasoning_effort=E] -
```

The prompt goes on standard input (`-`). `check_argv` refuses an argv that does not start with
`/usr/bin/sandbox-exec -f <profile>` and any argument starting with `--add-dir` (also
`--add-dir=/x`), `--approve-for-me` (which routes approvals through the workspace-write sandbox),
`--dangerously` or `--profile`, a `-p`, `danger-full-access` anywhere, and any `sandbox*`
configuration key (separate, `--config=`, or attached `-c`) other than the stage's own
`sandbox_workspace_write.exclude_slash_tmp=true`. `check_launch` also compares the argv with an
exact rebuild.

The hardening switches, each in `codex exec --help` or `codex features list` of 0.162.1, with its
config key read in the source at the tag:

| Switch | Closes |
|---|---|
| `--disable hooks`, `--disable plugins`, `--disable remote_plugin`, `--disable apps` | Hooks (including `CODEX_HOME/hooks.json`), local and remote plugins, the Apps connectors |
| `-c project_doc_max_bytes=0`, `-c project_root_markers=[]` | Project `AGENTS.md`; the walk above `-C` for project configuration |
| `-c analytics.enabled=false`, `-c web_search="disabled"` | Analytics; web search |
| `-c skills.bundled.enabled=false`, `-c skills.include_instructions=false` | Bundled skills (the `skills/.system` roots are then dropped); the skills instructions block |
| `-c shell_environment_policy.inherit=core` | Inherited secrets in model commands (see above) |

The fake `codex` of the tests refuses every flag, value and `-c` key outside this verified set,
so a misspelt switch fails the tests instead of passing silently.

## Environment

`codex_sandbox.environment` is an allowlist, separate from `runner.run_environment` (which copies
the owner's `HOME` and `TMPDIR`): `PATH`, `HOME`, `CODEX_HOME` and `TMPDIR` (the per-run
directories), `LANG`, `HTTPS_PROXY` and `HTTP_PROXY` (`http://127.0.0.1:<proxy port>`) and
`CODEX_API_KEY`. `check_environment` refuses any other name (and says when it names a secret), a
missing one, a `HOME`, `CODEX_HOME`, `TMPDIR` or proxy value other than the launch's, an empty
`CODEX_HOME`, a NUL, and any value other than the key that the outgoing-text scan flags.

**`PATH`** (`codex_sandbox.launch_path`) keeps only the runner's directories the profile lets the
process execute from (`/bin`, `/usr/bin`, `/usr/libexec`, the codex package and a stage's
interpreter roots), in their order, and adds `/usr/bin` and `/bin` when they are missing. A program
anywhere else cannot start under the profile (bash's 126), and an earlier one would hide the
system's: the round-2 review found a nix git first on the runner's `PATH`, so `git` by name
returned 126 under the stage profile while `/usr/bin/git` (the Command Line Tools' git through
`xcrun`) works. `check_environment` refuses a `PATH` that `launch_path` would change.

## The proxy

`codex_proxy.ConnectProxy` listens on 127.0.0.1 on an ephemeral port, started before the profile
is written (the port is a profile parameter and the proxy variables' value). It tunnels only
`CONNECT api.openai.com:443` (`codex_sandbox.allowed_hosts`; exact host, no subdomain), relays the
bytes without reading them (no TLS termination), answers `403` to every other target and to any
request that is not `CONNECT`, and records each requested `host:port` (at most 500) and the
connections it accepted, which the probes read. Its limits, because any local process can reach
the listener and a tunnel is a channel it cannot read:

| Limit | Value | Beyond it |
|---|---|---|
| Connections at a time (`MAX_CLIENTS`) | 16 | `503` |
| The whole request head (`HEADER_TIMEOUT`, one deadline, not per read) | 10 s | closed |
| Tunnels at a time (`MAX_TUNNELS`) | 8 | `503` |
| Idle tunnel (`IDLE_TIMEOUT`, no byte either way) | 300 s | closed (`ended["idle"]`) |
| Tunnel lifetime (`TUNNEL_SECONDS`) | 3600 s | closed (`ended["time"]`) |
| Bytes per tunnel (`TUNNEL_BYTES`) | 256 MiB | closed (`ended["bytes"]`) |

Whether a long real `codex exec` session stays within the idle and lifetime limits is not
verified (no model turn ran); codex would probably reconnect. Closing the proxy ends every
tunnel; the run uses it as a context manager, and `start` also closes it when the codex process
exits (`close_when`). The tests use only local listeners (an injected `connect`).

## Probes and the decision

`codex_probes.decide(codex, paths, ...)` returns a `Decision` with one of three outcomes:

| Outcome | Fields | The caller (#345) |
|---|---|---|
| Codex (`decision.codex`) | `client_applied: "codex"`, `launch` | starts it with `start` |
| Fallback (`decision.fallback`) | `client_applied: "claude"`, `reason` in `REASONS` | runs the Claude worker |
| **Abort** (`decision.abort`) | `abort: true`, `client_applied: null`, `reason` in `ABORT_REASONS` (`deny_probe_succeeded`) | **fails the step**; runs neither Codex nor the Claude worker (`start` refuses) |

A deny probe that got through, or a decoy read or leaked, is an abort, never a fallback, and turns
the schedule off (a decoy as a tripwire; since #350 any other deny-probe success as a failed
probe): the outer profile was shown to leak, so the step must not continue with another client.
The checks, in order:

| Check | Reason when it fails |
|---|---|
| macOS | `platform_unsupported` |
| The executable (tools.json, then `PATH`) | `codex_missing` |
| `codex --version` (scratch `CODEX_HOME` and `HOME`) is one of `VERIFIED_VERSIONS` (0.162.1) | `codex_version_unverified` |
| The run's `CODEX_HOME` allowlist | `codex_home_unclean` |
| `codex.login` set and its keychain item readable | `login_missing` |
| The launch builds, the proxy's allowlist is exactly the Codex hosts, `check_launch` passes | `profile_broken` |
| Model-free probes (below) | abort: `deny_probe_succeeded`; fallback: `profile_broken` (codex does not start under the profile), `venv_unusable` (a stage's environment does not run), `git_unusable` (a stage's `git` by name does not run), `probe_failed` |
| The nested-sandbox check | `nested_sandbox_unavailable` |

When several fail, the most damning reason wins: a deny-probe success first, then the order of the
table (`_RANK`). **Model-free probes** run with exactly the launch's profile prefix, environment
and working directory; only the command differs (`CodexLaunch.wrap`). Worker and stage share
most of them:

| Probe | Kind | Expected |
|---|---|---|
| `control.codex-start`: `codex --version` | both | runs (else `profile_broken`) |
| `control.proxy-port`: `nc -z 127.0.0.1 <proxy port>` | both | allowed, and the proxy counts the connection |
| `control.write-output`, `control.write-tmpdir` | both | allowed; the runner checks the files |
| `control.read-materials` (a runner-written seed), `control.list-tree` | worker | allowed |
| `control.read-exchange`, `control.write-repository`, `control.write-exchange` | stage | allowed |
| `control.venv-python`: `<venv>/bin/python -c 0` | stage | runs (else `venv_unusable`) |
| `control.git`: `git --version` by name | stage | runs (else `git_unusable`) |
| `read.<decoy>` for every file decoy (#343) and the ephemeral decoy in the owner's temporary directory | both | denied; the output is scanned and a marker trips the schedule |
| `list.ssh-dir`, `list.keychains-dir`, `list.secrets-dir` (`~/.config/agents-autodev`), `list.home`, `list.state-dir` (each when present) | both | denied; output discarded |
| `write.decoy-dir`, `write.private-dir` (a runner directory outside the run) | both | denied; the file stays absent |
| `write.tree`, `write.materials` | worker | denied |
| `write.read-root-<n>` for every read root (a stage's state copy) | both | denied |
| `write.venv` | stage | denied |
| `write.repo-git-config` (append), `write.repo-git-hooks`, `write.repo-git-rename` | stage | denied; config and hooks unchanged |
| `keychain.decoy`: `security find-generic-password -s <decoy> -a <account> -w <home>/Library/Keychains/login.keychain-db` | both, when the decoy keychain item exists | denied with an exit code other than 44 (not found); its value is scanned |
| `network.direct`: `nc` to the external host's address, resolved by the runner | both | denied (a runner that cannot resolve it fails with `probe_failed`, "untested") |
| `network.proxy-refused`: `curl -x <proxy> https://example.com/` | both | denied, and the proxy's log shows `example.com:443` refused |
| `network.local-listener`: `nc -z` to a second runner-owned listener | both | denied, and the listener counts no connection |
| `launchd.submit`: `launchctl submit` of a job that would create a canary | both | denied; no canary, and `launchctl list <label>` outside the sandbox finds no job |

Exit codes 126 and 127, a timeout, a missing proxy record or a proxy connection the proxy did not
count make a probe `inconclusive` or `failed`, never denied. Detection is the runner's (exit
codes, files, the proxy's log, the listener's count, launchd); captures are bounded and scanned
(the decoy markers and the run's API key, `Decision.known`), and only verdicts, exit codes and hit
metadata reach the record. Any deny-probe success aborts the step (`deny_probe_succeeded`,
`decision.abort`) and turns the schedule off (`decoys.trip`, `cause: probe`, #350); a decoy read or
leaked turns it off as a tripwire and is an abort too, whatever else failed. The decoy keychain
item is probed only when `sandbox.decoy_keychain` planted it (an absent item would "deny" by not
being found); the record then says `keychain: untested`, which does not fail the worker. Real
keychain items are never probed by a worker.

The **nested check** runs `codex sandbox -- /usr/bin/true` under the outer profile, only after
`control.codex-start` passed: exit 0 means the inner sandbox applied; anything else (here exit 71)
is `nested_sandbox_unavailable`.

`record_decision(run_id, directory, step, decision)` appends to the run record's `clients` list:
`step`, `client_requested`, `client_applied` (`null` for an abort), `fallback_reason` (`null`
for an abort), `aborted`, `abort_reason`, `detail`, the launch's
description (`codex_launch`: digests, shape, flags without paths; only for a Codex decision) and
the probe report (`codex_probes`, with its digest), masked with the decoy markers and the API key.

`start(decision, prompt, proxy=...)` refuses an aborted decision and starts Codex only for a
Codex decision whose probes passed at most 15 minutes ago for the same digest (argv,
environment, working directory, profile, executable), with its proxy running with the Codex
hosts, `check_launch` passing again and the
`CODEX_HOME` allowlist checked once more right before the process starts. The worker launcher
(#345) and the run sequence's Codex select stage (#347) call it;
`run_exec_probe` (below) is the only other place that starts codex for a model turn.

### Fallback reasons

`REASONS`: `platform_unsupported`, `codex_missing`, `codex_version_unverified`,
`codex_home_unclean`, `login_missing`, `profile_broken`, `nested_sandbox_unavailable`,
`probe_failed`, `venv_unusable`, `git_unusable`, `workspace_write_network_open`,
`stage_probe_failed`. The abort reason (`ABORT_REASONS`) is `deny_probe_succeeded`, never a
fallback. `read_availability` adds `not_checked`, `version_changed`, `shape_changed` and `stale`
for a stored stage check that no longer applies (a stored abort reads as unavailable with
`deny_probe_succeeded`).

## Codex as a stage client (Q14)

`stage_availability` decides Q14 and stores the result in
`<state>/schedules/codex-availability.json`. It needs:

1. the stage launch passing `decide` (model-free probes and the nested check);
2. the installed `workspace-write` blocking the network (`installed_checks`, below);
3. then, and only then, **the #343 probe plan through `codex exec`** (`run_exec_probe`, a model
   turn) with exactly the stage argv and environment: runner-written scripts in the exchange
   directory, each run as `/bin/bash <script>`, their `__probe__ <id> rc=<code>` lines read from
   the `--json` events (`item.completed` with `command_execution`; the event format is from
   `exec/src/exec_events.rs` at the tag).

The stage plan (`exec_plan`) keeps #343's check IDs and research groups:

| Group | Checks | Expected |
|---|---|---|
| C11, C15 controls | `control.exchange.script`, `control.repo.script`, `control.tmpdir.script`, `control.seed.script`, `control.git.script` (`git --version` by name; `git_unusable` when it runs and fails); the negative control `control.sandbox-loaded.script` | allowed; the negative control denied |
| C1, C15 reads | `read.<decoy>.script`, `list.ssh-dir`, `keychains-dir`, `home`, `secrets-dir`, `state-dir`, `owner-temp`, the decoy registry | denied |
| C2 | `read.git-credentials.script` (when present); `git.config-global.script` | denied; the exit code of `/usr/bin/git config --global --list` recorded |
| C3 | `keychain.<item>.script` for the runner's keychain items that exist (exit code only), `keychain.decoy.script` (its value) | denied with a code other than 44; none at all is "C3 untested", a failure |
| C4 | `gh.token.script`: the `gh` the runner finds on its own `PATH` (the launch's holds only executable directories), run by absolute path, `auth token` | denied (a gh the profile cannot start counts as denied, `EXEC_REFUSED`); no gh on the runner's `PATH` is recorded as untested |
| C5 | `network.external`, `network.external-noproxy` (must fail on name resolution), `network.metadata`, `network.dns` (the system resolver), `network.udp` (`dig` to 1.1.1.1) | denied |
| C6 | `network.local-listener` (the runner's listener counts no connection), `network.daemon` (when known), `network.listen` (a listen on 127.0.0.1 with the venv's Python) | denied |
| C8 | `socket.foreign.script` (a runner-owned Unix socket in a private directory; it counts no connection), `socket.extra-<n>.script` (`/var/run/docker.sock` when present) | denied |
| C9 | `launchd.submit.script` (no canary, no job), `apple-events.osascript.script`, `launch-services.open.script` | denied (osascript cannot start under the profile: `EXEC_REFUSED`) |
| C10, C12 writes | `write.<place>.script` for the home, the state directory, the owner's temporary directory, the private directory, the runner's records, the installation, `schedules/`, the trusted repository and its hooks, the plist's directory, every read root, the venv, `.git/config`, `.git/hooks`; `append.config-file`, `append.plist` | denied; no canary, appended files unchanged |
| C18 links | `link.symlink-read.script` (a link in the exchange directory to the credentials decoy), `link.hardlink.script`, `link.dir-symlink.script` (a new file through a link to the decoy directory) | denied |
| C13 | `observe.env.script`: the variable names a model command sees | none of `CODEX_API_KEY`, `HTTPS_PROXY`, `HTTP_PROXY` (any case) and no name with `TOKEN`, `KEY`, `SECRET` or `PASSWORD`; else `deny_probe_succeeded` |

`EXEC_REFUSED` (77) is the probes' own exit code for bash's 126 ("cannot execute") of a program
the runner found executable outside the sandbox: there 126 means the profile refused the exec.
Every other 126 stays inconclusive. A command that is not a planned script (after unwrapping
`bash -lc '...'`, whose exact form in the events is not verified), any other item kind (a file
change, a web search, an MCP call), a script that did not run or changed during the session, a
`turn.failed` or `error` event, output beyond the capture limit, a timeout or a scan hit fails the
check. Codex's own warning items (`item.type == "error"`) are kept as bounded, scanned warnings
(`exec_session.warnings`), not failures.

Not ported from #343, because they test the Claude CLI rather than the sandbox: the bypass
parameter (C7, `dangerouslyDisableSandbox`; the Codex argv refuses every bypass flag), the
project settings file (C16, `.claude`), the file-tool vectors (Read, Grep, Glob, Write, Edit),
the `defaults read` record (`prefs.read`), and the Claude directories (configuration, `gh`
configuration, settings file, Claude's temporary roots, the runner socket). `observe.env.script`
prints #343's whole observation (also `$TMPDIR` and the descriptors), but only the variable names
are judged. No probe was left out for any other reason.

Codex stays unavailable with the reason (`stage_probe_failed`, `git_unusable`,
`workspace_write_network_open` or a `decide` reason; a deny-probe success or a decoy read is an
abort with `deny_probe_succeeded`) unless every probe is denied and every control passed. On this
Mac step 1 already fails, so no model turn is spent.

**The stored availability** holds `available`, `reason`, `checked` and `checked_at`, the codex
version, the launch digest, the launch's run-independent `shape` and the installed checks, also on
a fallback (`complete_installed`). The shape (`codex_sandbox.shape_of`) covers the kind, the
profile template (rendered with one read root, interpreter root and ancestor, not the run's
layout), the codex flags without paths, the environment's variable names and the executable's
resolved path and SHA-256. `codex_sandbox.launch_shape(kind, codex, model=..., effort=...)`
computes it from the executable alone (placeholder paths, no key, no run directory, no profile
file), and `CodexLaunch.shape()` of a built launch is equal; `codex_probes.stage_shape(codex,
config, stage_kind)` takes the model and effort from `autodev.json` (`codex_settings(config,
"stage", stage_kind)`: `stages.<stage_kind>.codex`, `implement` when no kind is named, else
`delegates.code-review-pass.codex`). The `codex-probe` check probes the stage the configuration
runs with Codex (`runner.codex_stage_kind`: the first stage whose client is `codex`, else
`implement`). Since #347 a stage launch carries `--output-schema <file>`, the stage's schema
(`stages.write_schema`) inside its read-only state copy, so a stored availability from before is
`shape_changed` once.

`read_availability(directory, kind, version=..., shape=...)` treats it as unavailable when it was
never checked or the stored entry is not an object (`not_checked`), when the check failed (its
reason), when the installed version differs (`version_changed`), when the shape differs
(`shape_changed`, for example another effort in `autodev.json` or an upgraded executable), or
when it is older than `AVAILABILITY_MAX_AGE` (7 days) or dated in the future (`stale`). The run
sequence (#347) and the settings page (#348) call it without a key or a run:

```text
codex_probes.read_availability(directory, version=codex_probes.installed_version(codex, path=...),
                               shape=codex_probes.stage_shape(codex, config, "select"))
```

`installed_version` runs `codex --version` with a scratch `CODEX_HOME` and `HOME`;
`stage_shape` raises `CodexSandboxError` when the executable cannot be read (Codex is then
unavailable). A stage launch uses `codex_settings(config, "stage", kind)`, or its shape differs
from the stored one.

## Installed-version checks

`installed_checks(codex, path=...)` runs model-free and unnested, with a fresh scratch
`CODEX_HOME` and `HOME`, a runner-written file and a local listener only: the version (`codex
--version`), the effort list from the source (`efforts`, the constant), whether `codex sandbox`
(read-only by default) reads a file outside its working directory (`read_only_restricts_reads`;
the research: it does not restrict), and whether `codex sandbox -c
sandbox_mode="workspace-write" -- nc ...` reaches the listener (`workspace_write_blocks_network`;
the research: it is blocked). Each result is `null` when its controls fail (`codex sandbox --
/bin/echo ok` must run; a plain `nc` must reach the listener).

## Command

```text
python -m src.schedules codex-probe [--kind worker|stage] --confirm-live-probe
```

It refuses without the flag. `worker` builds a launch with a synthetic tree, materials and schema
in a private directory outside the state (`/private/tmp/autodev-codex-work-*`, removed afterwards)
and runs `decide` plus `installed_checks`; `stage` uses a one-off repository at `pinned` (like
`probe`) with the state copy as a read root and the trusted venv, and runs `stage_availability`.
The result is a run record with `client_requested`, `client_applied`, `fallback_reason`,
`aborted`, `abort_reason` and the report, masked with the decoy markers and the API key; the exit
code is 0 only when Codex is available.

## Residual risks

What the design does not close, and what bounds each:

- **Model commands can observe the codex process's environment, so `CODEX_API_KEY` is reachable
  by them.** `inherit=core` keeps the key out of what a model command inherits, but a process
  under the same profile can still learn the environment of the codex process it runs under; the
  sysctl name allowlist did not stop that in testing on macOS 26.5.1. No probe reads another
  process's environment or arguments. Mitigations: the key belongs to a separate OpenAI project
  with a spending limit, never the owner's account; the proxy reaches
  `api.openai.com` only, so the key is usable from the sandbox only against the API it is for;
  the key is a known secret of the run (`Decision.known`): every probe capture is scanned for it,
  the run record is masked with it (`record_decision`, the runner's markers), and the broker's
  outgoing-text scan refuses any text that holds it: #345 returns `decision.known` and forwards it
  through the worker service's `on_known` to `Broker.add_known`, which the run sequence (#347)
  wires up.
  A leaked key costs at most the project's budget until the owner revokes it.
- **The allowed host is a data channel.** Tunnels to `api.openai.com` are TLS the proxy cannot
  read, and model commands share the profile, so they can reach the proxy port too. Anything a
  model command can read (the tree, the materials, a stage's repository) could leave through
  requests to the OpenAI API, with the run's key or any key the command holds. The proxy limits
  (above) bound the volume and duration, not the content; the readable data is what the step
  needs anyway.
- **Global `file-read-metadata`.** Outside the home directory a model command can learn whether a
  path exists and its size, times and type (no content); codex needs metadata of many system
  paths at start. Inside the home directory only the ancestors of the run's directories are
  visible.
- The reads the outer profile still allows (the system directories, the Command Line Tools, the
  codex package, the run's directories) hold no secret of the owner; a read root is a
  runner-made copy (`CodexPaths.problems` refuses one that contains a protected place or lies
  inside `~/.ssh`, `~/Library`, the secrets directory or the state directory).

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
  the allowed host (see [Residual risks](#residual-risks); the key is reachable either way);
- the read-only versus workspace-write distinction moves into the outer profile (a worker's tree
  read-only, a stage's repository writable, `.git` protected), so a profile bug changes what Codex
  may write;
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
- **The host a real `codex exec` needs** with an API key (`api.openai.com`) is from the source,
  not observed.
- **The decoy keychain item's exit code.** What `security find-generic-password` returns under
  the profile for an item that exists was not measured (the tests never touch the real keychain).
  If it is 44, `keychain.decoy` is inconclusive and the worker falls back with `probe_failed`
  (fail closed).
- **Event commands and warnings.** How `codex exec --json` reports a command (`bash -lc '...'` or
  the plain string) and the shape of its warning and `turn.failed` events are from the source, not
  observed; an unrecognised command form fails closed.
- **The `CODEX_HOME` names of a real session** (`log/`, `state_<n>.sqlite`, `logs_<n>.sqlite`)
  are from the source; `codex --version` and `codex sandbox` create only `tmp/arg0`. A second start
  in one run after a session that creates other names would be refused.
- **The proxy limits against a long real session** (above).
- **Paths deeper in the real home.** The ancestor metadata rules were tested four levels below a
  fake home, not against the real `/Users/<owner>`.
- **`installed_checks` with the real codex** ran only with the fake in the tests; the expected
  values come from the research's runs.

## Files

| Path | What |
|---|---|
| `/private/tmp/ad-<8 hex>/{home,codex-home,tmp}` | A Codex process's `HOME`, `CODEX_HOME` and `TMPDIR` (0700), removed with the run's temporary root |
| `schedules/codex-availability.json` | Whether Codex is available as a stage client: `available`, `reason`, `checked`, `checked_at`, `version`, `digest`, `shape`, the installed checks |
| `schedules/runs/<run-id>/codex-profile/codex.sb` | A check's outer profile (0600) |
| `schedules/runs/<run-id>/codex-out/` | A check's output directory |
| `/private/tmp/autodev-codex-work-*/` | The worker check's synthetic tree, materials and schema (outside the state directory), removed after the check |
