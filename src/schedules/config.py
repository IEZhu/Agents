"""``<state_dir>/schedules/autodev.json``: the autodev schedule's configuration (design 10.3).

The file stays on this machine and is never synced. A missing file means the defaults; a file
without some keys gets them from the defaults, so a newer runner can add keys. Every load and
save is validated, and a save is atomic (`src.daemon.state.write_json`).

``clone`` is the runner's trusted repository (`src.schedules.repos`, design 5.4): an absolute
path, or one that starts with ``~``, that lies neither under ``~/Documents`` nor under the
installation, nor above either of them.
"""
from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any

from src.daemon.state import private_dir, read_json, write_json

from .lease import schedules_dir

CONFIG_NAME = "autodev.json"
MODES = ("dry-run", "run")
CLIENTS = ("claude", "codex")
STAGES = ("select", "implement", "submit")
DELEGATES = ("code-review-pass", "change-premortem")
LIMITS = ("max_reviews", "max_bot_rounds", "max_wait_minutes")
MAX_CATCH_UP_MINUTES = 24 * 60
# The installation this module runs from: src/schedules/config.py is two levels below it.
INSTALLATION = Path(__file__).resolve().parents[2]

DEFAULTS: dict[str, Any] = {
    "id": "autodev",
    "enabled": False,
    "mode": "dry-run",
    "repository": "IEZhu/Agents",
    "clone": "~/.local/share/agents-autodev/IEZhu-Agents",
    "times": ["02:17", "13:47"],
    "catch_up_minutes": 120,
    "allowed_hours": "08:00-23:00,00:00-06:00",
    "require_ac_power": True,
    "stages": {
        "select": {"client": "claude", "claude": {"model": "opus", "effort": "high"}},
        "implement": {"client": "claude", "claude": {"model": "opus", "effort": "xhigh"},
                      "budget": {"timeout_minutes": 180}},
        "submit": {"client": "claude", "claude": {"model": "opus", "effort": "high"}},
    },
    "delegates": {
        "code-review-pass": {"client": "codex", "codex": {"model": "gpt-6.1-sol", "effort": "high"},
                             "effort_followup": "medium"},
        "change-premortem": {"client": "claude", "claude": {"model": "opus", "effort": "high"}},
    },
    "limits": {"max_reviews": 3, "max_bot_rounds": 3, "max_wait_minutes": 120},
    "github": {
        "deploy_key": "~/.config/agents-autodev/deploy_key",
        "token": "keychain:agents-autodev-github",
    },
    # The session sandbox (#343, docs/autodev-sandbox.md): "config-dir" (a persistent
    # CLAUDE_CONFIG_DIR the owner logs in once) or "keychain:<service>" (an OAuth token read from
    # that keychain item); decoy_keychain plants the opt-in decoy keychain item.
    "sandbox": {"login": "config-dir", "decoy_keychain": False},
}

_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class ConfigError(ValueError):
    """``autodev.json`` is unreadable or invalid; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


def config_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / CONFIG_NAME


def minutes(text: str) -> int:
    """``HH:MM`` as minutes after midnight; ``ValueError`` otherwise."""
    match = _HHMM.match(text) if isinstance(text, str) else None
    if not match:
        raise ValueError(f"{text!r} is not a time of day HH:MM")
    return int(match[1]) * 60 + int(match[2])


def hour_ranges(text: str) -> list[tuple[int, int]]:
    """``"08:00-23:00,00:00-06:00"`` as (start, end) minutes; a range may cross midnight."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("allowed_hours must be ranges such as 08:00-23:00,00:00-06:00")
    ranges = []
    for part in text.split(","):
        start, separator, end = part.strip().partition("-")
        if not separator:
            raise ValueError(f"{part.strip()!r} is not a range HH:MM-HH:MM")
        ranges.append((minutes(start.strip()), minutes(end.strip())))
    return ranges


def runner_location(value: Any, installation: str | Path | None = None, *, what: str = "clone") -> Path:
    """``value`` as a resolved absolute path for the runner's own files (the trusted repository,
    its environments); ``ValueError`` when it is relative, or under or above ``~/Documents`` or
    the installation, where a session or the daemon's checkout could reach it."""
    if not _text(value):
        raise ValueError(f"{what} must be a path")
    path = Path(os.path.expanduser(str(value)))
    if not path.is_absolute():
        raise ValueError(f"{what} must be an absolute path or start with ~")
    resolved = Path(os.path.realpath(path))
    for name, root in (("~/Documents", Path.home() / "Documents"),
                       ("the installation", Path(installation) if installation is not None else INSTALLATION)):
        root = Path(os.path.realpath(root))
        if resolved.is_relative_to(root) or root.is_relative_to(resolved):
            raise ValueError(f"{what} must lie outside {name} ({root}): {resolved}")
    return resolved


def _merge(defaults: dict, values: dict) -> dict:
    merged = copy.deepcopy(defaults)
    for key, value in values.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _profile(where: str, profile: Any, problems: list[str], extra: tuple[str, ...]) -> None:
    if not isinstance(profile, dict):
        problems.append(f"{where} must be an object")
        return
    client = profile.get("client")
    if client not in CLIENTS:
        problems.append(f"{where}.client must be one of {', '.join(CLIENTS)}")
        return
    settings = profile.get(client)
    if not isinstance(settings, dict) or not _text(settings.get("model")) or not _text(settings.get("effort")):
        problems.append(f"{where}.{client} needs a model and an effort")
    unknown = set(profile) - {"client", *CLIENTS, *extra}
    if unknown:
        problems.append(f"{where} has unknown keys: {', '.join(sorted(unknown))}")
    budget = profile.get("budget")
    if budget is not None and (not isinstance(budget, dict) or not _positive_int(budget.get("timeout_minutes"))):
        problems.append(f"{where}.budget.timeout_minutes must be a positive integer")
    if "effort_followup" in profile and not _text(profile["effort_followup"]):
        problems.append(f"{where}.effort_followup must be a non-empty string")


def validate(config: Any) -> dict:
    """``config`` unchanged when valid; ``ConfigError`` naming every problem otherwise."""
    if not isinstance(config, dict):
        raise ConfigError(["autodev.json must hold a JSON object"])
    problems: list[str] = []
    unknown = set(config) - set(DEFAULTS)
    if unknown:
        problems.append(f"unknown keys: {', '.join(sorted(unknown))}")
    if not isinstance(config.get("id"), str) or not _ID.match(config["id"]):
        problems.append("id must be lowercase letters, digits and hyphens")
    for key in ("enabled", "require_ac_power"):
        if not isinstance(config.get(key), bool):
            problems.append(f"{key} must be true or false")
    if config.get("mode") not in MODES:
        problems.append(f"mode must be one of {', '.join(MODES)}")
    if not isinstance(config.get("repository"), str) or not _REPOSITORY.match(config["repository"]):
        problems.append("repository must be OWNER/NAME")
    try:
        runner_location(config.get("clone"))
    except ValueError as error:
        problems.append(str(error))
    times = config.get("times")
    if not isinstance(times, list) or not times:
        problems.append("times must be a non-empty list of HH:MM")
    else:
        for value in times:
            try:
                minutes(value)
            except ValueError as error:
                problems.append(f"times: {error}")
        if len(set(map(str, times))) != len(times):
            problems.append("times must not repeat")
    catch_up = config.get("catch_up_minutes")
    if not _positive_int(catch_up) or catch_up > MAX_CATCH_UP_MINUTES:
        problems.append(f"catch_up_minutes must be an integer from 1 to {MAX_CATCH_UP_MINUTES}")
    try:
        hour_ranges(config.get("allowed_hours"))
    except ValueError as error:
        problems.append(f"allowed_hours: {error}")
    stages = config.get("stages")
    if not isinstance(stages, dict) or set(stages) != set(STAGES):
        problems.append(f"stages must have exactly {', '.join(STAGES)}")
    else:
        for name in STAGES:
            _profile(f"stages.{name}", stages[name], problems, ("budget",))
    delegates = config.get("delegates")
    if not isinstance(delegates, dict) or not set(delegates) <= set(DELEGATES):
        problems.append(f"delegates may have only {', '.join(DELEGATES)}")
    else:
        for name, profile in delegates.items():
            _profile(f"delegates.{name}", profile, problems, ("effort_followup", "budget"))
    limits = config.get("limits")
    if not isinstance(limits, dict) or set(limits) != set(LIMITS) or \
            not all(_positive_int(limits[name]) for name in LIMITS):
        problems.append(f"limits must have positive integers {', '.join(LIMITS)}")
    github = config.get("github")
    if not isinstance(github, dict) or set(github) != {"deploy_key", "token"}:
        problems.append("github must have deploy_key and token")
    else:
        if not _text(github["deploy_key"]):
            problems.append("github.deploy_key must be a path")
        token = github["token"]
        if not isinstance(token, str) or not token.startswith("keychain:") or not token[len("keychain:"):].strip():
            problems.append("github.token must name a keychain item: keychain:<service>")
    sandbox = config.get("sandbox")
    if not isinstance(sandbox, dict) or set(sandbox) != {"login", "decoy_keychain"}:
        problems.append("sandbox must have login and decoy_keychain")
    else:
        login = sandbox["login"]
        if not (login == "config-dir" or (isinstance(login, str) and login.startswith("keychain:")
                                          and login[len("keychain:"):].strip())):
            problems.append("sandbox.login must be config-dir or keychain:<service>")
        if not isinstance(sandbox["decoy_keychain"], bool):
            problems.append("sandbox.decoy_keychain must be true or false")
    if problems:
        raise ConfigError(problems)
    return config


def load(directory: str | Path | None = None) -> dict:
    """The validated configuration: the file merged over the defaults, or the defaults alone."""
    path = config_path(directory)
    try:
        stored = read_json(path, None)
    except (OSError, ValueError) as error:
        raise ConfigError([f"{path} cannot be read: {type(error).__name__}"]) from None
    if stored is None:
        return copy.deepcopy(DEFAULTS)
    if not isinstance(stored, dict):
        raise ConfigError(["autodev.json must hold a JSON object"])
    return validate(_merge(DEFAULTS, stored))


def save(config: dict, directory: str | Path | None = None) -> Path:
    """Validate and write ``config`` atomically into a private directory."""
    validate(config)
    path = config_path(directory)
    private_dir(path.parent)
    write_json(path, config)
    return path
