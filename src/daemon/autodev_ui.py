"""The Autodev page of the web UI (#348): the settings of the autodev schedule.

An early, autodev-only slice of the Schedules tab of #211. The page edits
``<state_dir>/schedules/autodev.json`` only through `src.schedules.config` (``load``, ``validate``
and ``save``, which writes atomically into a private directory) and turns the schedule on and off
only through the runner's own ``enable`` and ``disable`` (`src.schedules.runner`), the functions
behind ``python -m src.schedules autodev enable|disable``. ``FlowsUI`` hands a request here only
after its own checks: the loopback ``Host``, a valid session cookie and the ``X-Agents-UI`` header
with a same-origin ``Origin``, on GET as well. Stage sessions cannot reach the page: they have no
network access (#343), and the state directory is closed to them.

- ``GET /ui/api/autodev``: the settings the page edits (`editable`: the schedule, the limits, and
  the client, model and effort of every stage and delegated step, ``effort_followup`` and the
  pre-mortem role), the defaults, the configuration's ``revision`` (`revision`: the loaded
  configuration without ``enabled`` and ``mode``, so the switch and the tripwire leave it as it
  was), the effort lists of the capability
  table (`src.flow_execution.installed`, kept for `CAPABILITY_TTL` seconds), the agents a role
  may name, whether Codex can be a stage client (`codex_probes.configured_stage_availability`,
  as the run sequence asks; implement and submit commit, which a Codex stage cannot) and the
  status: on or off, the mode, the next slot (`tick.next_slot`), whether the scheduler job is
  installed, the newest runs (`runner.run_summary` of the run records) and the automatic mode
  (#350, ``automatic``): whether ``autodev ready`` passes (`runner.readiness`, with the
  preconditions it reads from GitHub kept for `READY_TTL` seconds), what is missing, when the
  owner's acceptance was given and the risks it covers. A file that does not validate gives
  ``problems`` and no settings.
- ``PUT /ui/api/autodev`` with ``{expected_revision, settings}``: the settings applied over the
  stored configuration (keys left out keep their values; the stage ``budget`` and the GitHub,
  sandbox and Codex login settings are never touched), then checked by ``config.validate`` and
  by `src.flow_execution` (``check_effort`` against the table, ``check_model``; a model warning
  does not block) and the role against the agents, then saved by ``config.save``. 400
  ``invalid_settings`` lists every problem and leaves the file as it was; 409 ``config_changed``
  when the configuration changed since the page read it. ``enabled`` and ``mode`` change only
  with the switch: a save takes them from the file as it is when it writes.
- ``POST /ui/api/autodev/enable`` with ``{mode, accept_shared_account}``: ``runner.enable``, as
  ``autodev enable`` runs it. ``run`` (the automatic mode, #350) is refused with 409
  ``acceptance_required`` without ``accept_shared_account: true`` and 409 ``not_ready`` while a
  condition is missing; otherwise the owner's acceptance is recorded (``source: page``). The page
  offers ``run`` only while ``autodev ready`` passes, the acceptance included, so the first
  acceptance is the CLI's.
- ``POST /ui/api/autodev/disable``: ``runner.disable``.

No answer carries a secret: the GitHub token and deploy key, the sandbox and Codex logins and
the executables' paths stay out, and the run summaries and the missing items are masked
(`records.mask`, with the paths of the deploy key and of ``~/.config/agents-autodev`` as known
secrets). The automatic mode's precondition reads take the broker's token from the keychain in
the daemon, as ``autodev ready`` does; only the names of what is missing are kept. Every request
runs in a worker thread. A save holds ``config.writing`` (the lock the runner's ``enable`` and
``disable`` and the decoy tripwire take too) from its load, the revision check included, to its
write, so it never writes back ``enabled`` or ``mode`` loaded before another writer's save.
The runner and the Codex modules load with the page's first request, not with the daemon.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.flow_execution import CLIENTS, FlowInvalid, check_effort, check_model
from src.schedules import records
from src.schedules.config import DEFAULTS, DELEGATES, LIMITS, MODES, STAGES, ConfigError
from src.schedules import config as autodev_config
from src.schedules.tick import iso, next_slot

PREFIX = "/ui/api/autodev"
MAX_BODY = 64 * 1024
RUNS_SHOWN = 10
CAPABILITY_TTL = 600.0  # seconds the CLIs' effort lists and the codex version are kept
READY_TTL = 60.0  # seconds the automatic mode's preconditions read from GitHub are kept (#350)
INSTALLATION = Path(__file__).resolve().parents[2]
EDITABLE = ("times", "catch_up_minutes", "allowed_hours", "require_ac_power", "limits", "stages", "delegates")
SWITCHED = ("enabled", "mode")
PREMORTEM = "change-premortem"
# The delegated steps with later passes, which take effort_followup (the later reviews and the
# verification review, `src.schedules.workers`); a pre-mortem runs once, so the run never reads its
# effort_followup, and a stored one is not checked against its client.
FOLLOWUPS = ("code-review-pass",)
CLIENT_KEYS = ("model", "effort")
SECRETS_DIR = "~/.config/agents-autodev"
# Why Codex cannot be a stage client, in words: every reason `codex_probes` stores (REASONS and
# ABORT_REASONS) or `read_availability` gives, and the stages that commit (`stages`).
REASONS = {
    "not_checked": "the stage probes have not passed yet: run `python -m src.schedules codex-probe --kind stage "
                   "--confirm-live-probe`",
    "version_changed": "the last check was for another codex version; check again",
    "shape_changed": "the last check was for other Codex settings of this stage; check again",
    "stale": "the last check is too old (7 days at most) or from the future; check again",
    "codex_stage_commit_unsupported": "this stage commits, and a Codex stage cannot (workspace-write keeps .git "
                                      "read-only)",
    "platform_unsupported": "the Codex sandbox runs on macOS only",
    "codex_missing": "the codex executable was not found (tools.json, PATH)",
    "codex_version_unverified": "this codex version is not one the Codex sandbox was verified with",
    "codex_home_unclean": "the run's CODEX_HOME holds something codex did not create itself, which it could load",
    "login_missing": "no Codex API key: codex.login is not set, or its keychain item does not hold a key",
    "profile_broken": "codex does not start under the sandbox profile",
    "nested_sandbox_unavailable": "a nested sandbox cannot apply on this macOS",
    "probe_failed": "a model-free probe of the sandbox failed or could not run; see the check's report",
    "venv_unusable": "the stage's Python environment does not run under the sandbox profile",
    "git_unusable": "git does not run under the sandbox profile",
    "workspace_write_network_open": "the Codex sandbox in workspace-write mode did not block network access",
    "stage_probe_failed": "the stage probe session failed; see the check's report",
    "deny_probe_succeeded": "a probe the sandbox must deny got through, or a decoy was read or leaked",
}


def is_autodev_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class AutodevError(Exception):
    """A refused request: ``code`` names it, ``status`` is the HTTP status, ``extra`` goes into the answer."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


def _runner():
    """The runner module, loaded with the page's first request; its POSIX-only parts (process groups,
    signals) do not load everywhere, and then the page says so instead of failing."""
    try:
        from src.schedules import runner
    except (ImportError, AttributeError) as error:
        raise AutodevError("platform_unsupported", f"the autodev runner does not load on this platform: "
                           f"{type(error).__name__}: {error}", 501) from None
    return runner


def default_context(service) -> Any:
    """The runner's context for the daemon's state directory, as the CLI builds it."""
    return _runner().Context(installation=INSTALLATION, directory=Path(service.directory))


def editable(config: Mapping) -> dict:
    """What the page edits, in the shape ``PUT`` takes back."""
    def profile(group: str, name: str) -> dict:
        stored = (config.get(group) or {}).get(name) or {}
        found: dict = {"client": stored.get("client")}
        for client in CLIENTS:
            settings = stored.get(client)
            found[client] = {key: settings.get(key) for key in CLIENT_KEYS} if isinstance(settings, dict) else None
        if group == "delegates":
            found["effort_followup"] = stored.get("effort_followup")
        if name == PREMORTEM:
            found["role"] = stored.get("role")
        return found

    return {"times": list(config["times"]), "catch_up_minutes": config["catch_up_minutes"],
            "allowed_hours": config["allowed_hours"], "require_ac_power": config["require_ac_power"],
            "limits": dict(config["limits"]),
            "stages": {name: profile("stages", name) for name in STAGES},
            "delegates": {name: profile("delegates", name) for name in DELEGATES}}


def _profile(where: str, stored: dict, given: Any, keys: tuple[str, ...], problems: list[str],
             default: Mapping) -> dict:
    """``stored`` with the keys ``given`` names; an empty client setting removes that client's object
    and an empty role the role (the default applies). An empty ``effort_followup`` is stored as null
    where the ``default`` profile has one (the later passes keep the first effort; without the key,
    `config.load` would merge the default's back), else removed."""
    if not isinstance(given, dict):
        problems.append(f"{where} must be an object")
        return stored
    unknown = sorted(set(given) - set(keys))
    if unknown:
        problems.append(f"{where} has unknown keys: {', '.join(unknown)}")
    profile = copy.deepcopy(stored)
    if "client" in given:
        profile["client"] = given["client"]
    for client in CLIENTS:
        if client not in given:
            continue
        value = given[client]
        if value is not None and (not isinstance(value, dict) or set(value) - set(CLIENT_KEYS) or
                                  not all(item is None or isinstance(item, str) for item in value.values())):
            problems.append(f"{where}.{client} must be an object with a model and an effort")
            continue
        settings = {key: value[key].strip() for key in CLIENT_KEYS
                    if value and isinstance(value.get(key), str) and value[key].strip()}
        if settings:
            profile[client] = settings
        else:
            profile.pop(client, None)
    for key in ("effort_followup", "role"):
        if key not in given:
            continue
        value = given[key]
        if value is None or (isinstance(value, str) and not value.strip()):
            if key == "effort_followup" and key in default:
                profile[key] = None
            else:
                profile.pop(key, None)
        elif isinstance(value, str):
            profile[key] = value.strip()
        else:
            problems.append(f"{where}.{key} must be a string")
    return profile


def apply(config: dict, settings: Any) -> dict:
    """A copy of ``config`` with the page's ``settings`` (`editable`'s shape, every key optional);
    `ConfigError` for keys or shapes the page does not send. The result is not validated yet."""
    if not isinstance(settings, dict):
        raise ConfigError(["settings must be an object"])
    problems: list[str] = []
    switched = [key for key in SWITCHED if key in settings]
    if switched:
        problems.append(f"{' and '.join(switched)} change only with the switch (turn on or off)")
    unknown = sorted(set(settings) - set(EDITABLE) - set(SWITCHED))
    if unknown:
        problems.append(f"settings has unknown keys: {', '.join(unknown)}")
    updated = copy.deepcopy(config)
    for key in ("times", "catch_up_minutes", "allowed_hours", "require_ac_power"):
        if key in settings:
            updated[key] = copy.deepcopy(settings[key])
    if "limits" in settings:
        limits = settings["limits"]
        updated["limits"] = {**updated["limits"], **limits} if isinstance(limits, dict) else limits
    for group, names in (("stages", STAGES), ("delegates", DELEGATES)):
        if group not in settings:
            continue
        given = settings[group]
        if not isinstance(given, dict):
            problems.append(f"{group} must be an object")
            continue
        for name, profile in given.items():
            if name not in names:
                problems.append(f"{group} has no {name!r}; it has {', '.join(names)}")
                continue
            keys = ("client", *CLIENTS) if group == "stages" else \
                ("client", *CLIENTS, "effort_followup", *(("role",) if name == PREMORTEM else ()))
            updated[group][name] = _profile(f"{group}.{name}", updated[group].get(name) or {}, profile, keys,
                                            problems, DEFAULTS[group][name])
    if problems:
        raise ConfigError(problems)
    return updated


def execution_problems(config: Mapping, table: Mapping, agents: set[str]) -> tuple[list[str], list[str]]:
    """Problems and warnings of every profile against `src.flow_execution` (each client's model and
    effort, ``effort_followup`` of the `FOLLOWUPS` for the profile's client) and of the role against
    ``agents``."""
    problems: list[str] = []
    warnings: list[str] = []

    def effort(where: str, client: str, value: Any) -> None:
        try:
            check_effort(client, value, table)
        except FlowInvalid as error:
            problems.append(f"{where}: {error} (allowed: {', '.join(error.allowed)})")

    for group, names in (("stages", STAGES), ("delegates", DELEGATES)):
        for name in names:
            profile = (config.get(group) or {}).get(name)
            if not isinstance(profile, dict):
                continue
            where = f"{group}.{name}"
            for client in CLIENTS:
                settings = profile.get(client)
                if not isinstance(settings, dict):
                    continue
                try:
                    warnings += [f"{where}.{client}.model: {text}" for text in check_model(settings.get("model"), client)]
                except FlowInvalid as error:
                    problems.append(f"{where}.{client}.model: {error}")
                effort(f"{where}.{client}.effort", client, settings.get("effort"))
            if name in FOLLOWUPS and profile.get("effort_followup") is not None and profile.get("client") in CLIENTS:
                effort(f"{where}.effort_followup", profile["client"], profile["effort_followup"])
            role = profile.get("role")
            if isinstance(role, str) and agents and role not in agents:
                problems.append(f"{where}.role: no agent named {role!r} (agents/<name>/system_prompt.mdc)")
    return problems, warnings


def revision(config: Mapping) -> str:
    """The revision of a loaded configuration (`config.load`: the file merged over the defaults, or
    the defaults): SHA-256 of its JSON without ``enabled`` and ``mode``. It describes exactly the
    configuration it was computed from; only a change to another key changes it, so the switch, the
    CLI's ``enable`` and ``disable`` and the tripwire leave a form's revision valid, and a file with
    the defaults has the revision of no file."""
    kept = {key: value for key, value in config.items() if key not in SWITCHED}
    text = json.dumps(kept, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def secret_paths(config: Mapping | None) -> list[str]:
    """The paths of the secret files, as written and expanded, longest first: masked in run summaries."""
    found = set()
    for value in {str(((config or {}).get("github") or {}).get("deploy_key") or ""),
                  DEFAULTS["github"]["deploy_key"], SECRETS_DIR}:
        if value:
            expanded = os.path.expanduser(value)
            found.update({value, expanded, os.path.realpath(expanded)})
    return sorted(found, key=len, reverse=True)


class AutodevUI:
    def __init__(self, service, context: Callable[[], Any] | None = None,
                 agents: Callable[[], set[str]] | None = None):
        self.service = service
        self.context = context or (lambda: default_context(service))
        self.agents = agents or _agents
        self._cache: tuple | None = None  # (expires, key, facts)
        self._github: tuple | None = None  # (expires, key, missing preconditions)

    async def handle(self, request: Request, path: str):
        routes = {PREFIX: {"GET": self.read, "PUT": self.save},
                  PREFIX + "/enable": {"POST": self.enable}, PREFIX + "/disable": {"POST": self.disable}}
        methods = routes.get(path)
        if methods is None:
            return self._json({"error": "not_found"}, 404)
        work = methods.get(request.method)
        if work is None:
            return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": ", ".join(methods)})
        body: dict = {}
        if request.method != "GET":
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}") if len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"status": "error", "error": "invalid_request",
                                   "message": "the body must be a small JSON object"}, 400)
        try:
            return self._json(await asyncio.to_thread(work, body))
        except AutodevError as error:
            return self._json({"status": "error", "error": error.code, "message": str(error), **error.extra},
                              error.status)
        except ConfigError as error:  # autodev.json changed under a write and no longer validates
            return self._json({"status": "error", "error": "invalid_config", "message": str(error),
                               "problems": error.problems}, 409)
        except OSError as error:
            return self._json({"status": "error", "error": "storage_error",
                               "message": f"storage_error: {error.strerror or error}"}, 500)
        except ValueError as error:  # a damaged tools.json or tick.json, a symbolic link in the state directory
            return self._json({"status": "error", "error": "state_error", "message": f"state_error: {error}"}, 500)

    @staticmethod
    def _json(value, status=200, **kwargs):
        headers = {"Cache-Control": "no-store", **kwargs.pop("headers", {})}
        return JSONResponse(value, status, headers=headers, **kwargs)

    # --- what the page shows -------------------------------------------------------------------------

    def _facts(self, ctx) -> dict:
        """The capability table and the codex executable, kept for `CAPABILITY_TTL` seconds: the
        table runs ``claude --version``, ``claude --help`` and ``codex --version`` (no model turn)."""
        from src import flow_execution
        runner = _runner()
        claude, codex = runner.claude_executable(ctx), runner.codex_executable(ctx)
        path = runner.run_environment(ctx)["PATH"]
        key = (claude, codex, path)
        cached = self._cache
        if cached is not None and cached[1] == key and cached[0] > time.monotonic():
            return cached[2]
        facts = {"table": flow_execution.installed(claude, codex, path=path, runner=ctx.command_runner),
                 "codex": codex}
        self._cache = (time.monotonic() + CAPABILITY_TTL, key, facts)
        return facts

    def _codex(self, ctx, config: Mapping, facts: dict) -> dict:
        """Whether Codex can be the client of each stage now, with the reason in words when not."""
        from src.schedules import codex_probes, stages
        version = facts["table"]["codex"].version
        found_stages = {}
        for kind in STAGES:
            if kind in stages.WRITING_KINDS:
                found = {"available": False, "reason": stages.CODEX_COMMIT_UNSUPPORTED, "checked": None}
            else:
                found = codex_probes.configured_stage_availability(ctx.directory, config, kind, facts["codex"],
                                                                   version=version)
            available = found.get("available") is True
            reason = None if available else str(found.get("reason") or "not_checked")
            checked = found.get("checked")
            found_stages[kind] = {"available": available, "reason": reason,
                                  "message": REASONS.get(reason, reason) if reason else None,
                                  "checked": checked if isinstance(checked, str) else None}
        return {"installed": bool(facts["codex"]), "version": version, "stages": found_stages}

    def _preconditions(self, ctx, config: Mapping) -> list[str]:
        """The real-run preconditions read from GitHub (`runner.github_conditions`: the keychain token
        and a few GETs), kept for `READY_TTL` seconds per repository and credentials setting."""
        key = (config["repository"], json.dumps(config["github"], sort_keys=True))
        cached = self._github
        if cached is not None and cached[1] == key and cached[0] > time.monotonic():
            return cached[2]
        found = _runner().github_conditions(ctx, config)
        self._github = (time.monotonic() + READY_TTL, key, found)
        return found

    def _automatic(self, ctx, config: Mapping) -> dict:
        """The automatic mode (#350): whether ``autodev ready`` passes, what is missing (masked),
        the acceptance and the risks it covers."""
        from src.schedules import automatic
        state = _runner().readiness(ctx, dict(config), github=self._preconditions(ctx, config))
        accepted = state.accepted or {}
        return {"ready": state.ready, "missing": records.mask(state.missing, secret_paths(config)),
                "accepted": accepted.get("accepted") if not state.acceptance else None,
                "risks": automatic.risks(), "risk_list_sha256": automatic.risk_list_version()}

    def _status(self, ctx, config: Mapping | None) -> dict:
        runner = _runner()
        known = secret_paths(config)
        runs = [records.mask(runner.run_summary(record), known)
                for record in records.all_records(ctx.directory, limit=RUNS_SHOWN)]
        return {"enabled": config["enabled"] if config else None, "mode": config["mode"] if config else None,
                "next_slot": iso(next_slot(config["times"], ctx.clock())) if config else None,
                "scheduler": {"installed": ctx.scheduler().plist.is_file(), "supported": ctx.platform == "darwin"},
                "runs": runs, "automatic": self._automatic(ctx, config) if config else None}

    def read(self, body: dict | None = None) -> dict:
        ctx = self.context()
        try:  # one read: the settings, the revision and the status describe the same file
            config, problems = autodev_config.load(ctx.directory), None
        except ConfigError as error:
            config, problems = None, error.problems
        facts = self._facts(ctx)
        table = facts["table"]
        return {
            "revision": revision(config) if config else "",  # an invalid file has no form to save
            "problems": problems,
            "settings": editable(config) if config else None,
            "defaults": editable(DEFAULTS),
            "choices": {"clients": list(CLIENTS), "modes": list(MODES), "limits": list(LIMITS),
                        "efforts": {client: list(capability.efforts) for client, capability in table.items()},
                        "agents": sorted(self.agents())},
            "capabilities": {client: capability.to_dict() for client, capability in table.items()},
            "codex": self._codex(ctx, config, facts) if config else None,
            "status": self._status(ctx, config),
        }

    # --- what the page changes ------------------------------------------------------------------------

    def save(self, body: dict) -> dict:
        expected = body.get("expected_revision")
        if not isinstance(expected, str):
            raise AutodevError("invalid_request", "expected_revision (the revision the page read) is required")
        unknown = sorted(set(body) - {"expected_revision", "settings"})
        if unknown:
            raise AutodevError("invalid_request", f"unknown fields: {', '.join(unknown)}")
        ctx = self.context()
        table, agents = self._facts(ctx)["table"], self.agents()  # before the lock: the table may run the CLIs
        # From the load to the write under the writers' lock: enabled and mode are the file's at the
        # write (a tripwire's off, the CLI's on or off), and only the page's keys change.
        with autodev_config.writing(ctx.directory):
            config = self._load(ctx)
            if revision(config) != expected:
                raise AutodevError("config_changed", "autodev.json changed since the page read it; reload to see it",
                                   409)
            try:
                updated = autodev_config.validate(apply(config, body.get("settings")))
            except ConfigError as error:
                raise self._invalid(error.problems) from None
            problems, warnings = execution_problems(updated, table, agents)
            if problems:
                raise self._invalid(problems)
            autodev_config.save(updated, ctx.directory)
        return {**self.read(), "saved": True, "warnings": warnings}

    def enable(self, body: dict) -> dict:
        mode = body.get("mode", "dry-run")
        if mode not in MODES:
            raise AutodevError("invalid_request", f"mode must be one of {', '.join(MODES)}")
        accept = body.get("accept_shared_account", False)
        if not isinstance(accept, bool):
            raise AutodevError("invalid_request", "accept_shared_account must be true or false")
        unknown = sorted(set(body) - {"mode", "accept_shared_account"})
        if unknown:
            raise AutodevError("invalid_request", f"unknown fields: {', '.join(unknown)}")
        runner = _runner()
        ctx = self.context()
        config = self._load(ctx)  # an invalid file is refused before anything is written
        # As `autodev enable` runs it (it takes the writers' lock itself); run checks every condition
        # afresh, then records the acceptance (#350).
        code, result = runner.enable(ctx, mode, accept_shared_account=accept, source="page")
        if code != 0:
            extra = {"risks": result["risks"]} if "risks" in result else {}
            raise AutodevError(result.get("error") or "refused", result.get("message") or "refused", 409,
                               missing=records.mask(result.get("missing") or [], secret_paths(config)), **extra)
        return {**self.read(), "result": result}

    def disable(self, body: dict) -> dict:
        runner = _runner()
        ctx = self.context()
        self._load(ctx)
        _, result = runner.disable(ctx)
        return {**self.read(), "result": result}

    @staticmethod
    def _load(ctx) -> dict:
        try:
            return autodev_config.load(ctx.directory)
        except ConfigError as error:
            raise AutodevError("invalid_config", "autodev.json is invalid; fix or remove it (then the defaults "
                               f"apply): {error.problems[0]}", 409, problems=error.problems) from None

    @staticmethod
    def _invalid(problems: list[str]) -> AutodevError:
        more = f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""
        return AutodevError("invalid_settings", f"not saved: {problems[0]}{more}", 400, problems=problems)


def _agents() -> set[str]:
    from src.component_catalog import list_agents
    return {agent["id"] for agent in list_agents()}
