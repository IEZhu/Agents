"""Which model a delegated worker runs on: tiers, floors, latest-model resolution, escalation.

The parent rates an assignment ``low``, ``standard`` or ``high``; that names a tier
(``light`` < ``standard`` < ``inherit``). Floors may raise the tier for risky or large
assignments and never lower it. A tier resolves per client to the newest model of its class
in the client's own catalog, a pinned fallback, or an ``AGENTS_SUBAGENT_*`` override;
``inherit`` sets no model, so the worker keeps the parent's. See docs/subagent-model-tiers.md.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging
import os
import re
from typing import Mapping

from src.engine import model_catalogs
from src.schemas.subagent import ContractExecution, Escalation, ExecutionPlan, PreparationRequest, Spawn

logger = logging.getLogger(__name__)

TIER_ORDER = ("light", "standard", "inherit")
COMPLEXITY_TIERS = {"low": "light", "standard": "standard", "high": "inherit"}
TIER_COMPLEXITY = {tier: complexity for complexity, tier in COMPLEXITY_TIERS.items()}

MAX_LIGHT_CRITERIA = 8
MAX_LIGHT_SCOPE = 8
MAX_LIGHT_CHARS = 12000
MAX_LIGHT_ARTIFACT_ENTRIES = 5
MAX_LIGHT_ARTIFACT_BYTES = 65536

# Assignments a wrong answer makes costly. False positives only cost tokens, so these lean wide;
# Russian stems cover parents that write assignments in Russian.
RISKS = {name: re.compile(pattern) for name, pattern in {
    "security": r"secur|vulnerab|exploit|\bcve-|threat model|безопасн|уязвим",
    "auth": r"authenticat|authoriz|\boauth|privilege|авториз|аутентиф",
    "secrets": r"credential|secret|password|api key|private key|парол|секрет",
    "crypto": r"encrypt|decrypt|cryptograph|шифр",
    "data": r"migration|\bmigrate|schema change|drop table|delete data|data loss|backfill|миграц",
    "production": r"production|\bdeploy|rollout|rollback|incident|продакшен|\bпрод\b|деплой|выкат",
    "infrastructure": r"terraform|kubernetes|\bhelm\b|infrastructure|\biam\b|инфраструктур",
    "concurrency": r"concurren|race condition|deadlock|thread.safe|гонк[аиу]|взаимоблокир",
    "architecture": r"architect|system design|design decision|архитектур",
}.items()}

_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\[\]-]{0,63}")
_EFFORT = re.compile(r"[a-z]{1,16}")
_VERSION = r"(\d+(?:\.\d+)*)"


@dataclass(frozen=True)
class Choice:
    """How one tier picks its model: a fixed name, or the newest catalog entry of a family."""
    model: str | None = None
    family: re.Pattern | None = None
    catalog: str | None = None  # a model_catalogs function, looked up at call time
    effort: str | None = None
    alias: bool = False


CHOICES = {
    # Claude Code's aliases always name the latest model of each class.
    "claude": {"light": Choice("haiku", alias=True), "standard": Choice("sonnet", alias=True)},
    "codex": {
        "light": Choice("gpt-6-luna", re.compile(rf"gpt-{_VERSION}-luna"), "codex_models", "medium"),
        "standard": Choice("gpt-6.1-sol", re.compile(rf"gpt-{_VERSION}-sol"), "codex_models", "medium"),
    },
    "cursor": {
        "light": Choice("default"),  # Auto, the first entry of Cursor's model list
        "standard": Choice("composer-2.5", re.compile(rf"composer-{_VERSION}"), "cursor_models"),
    },
}


@dataclass(frozen=True)
class Resolution:
    model: str | None
    effort: str | None
    source: str


def client_from_app(name: str | None) -> str | None:
    """The launching client for an ``X-Agents-Client`` app or MCP ``clientInfo.name``."""
    name = (name or "").strip().lower()
    if "codex" in name:
        return "codex"
    if "cursor" in name:
        return "cursor"
    if name == "claude" or name.startswith("claude-code"):
        return "claude"
    return None  # the Claude desktop chat, Antigravity and others launch no workers here


def _rank(tier: str) -> int:
    return TIER_ORDER.index(tier)


def artifact_floor(*, entries: int, size_bytes: int) -> list[str]:
    if entries > MAX_LIGHT_ARTIFACT_ENTRIES or size_bytes > MAX_LIGHT_ARTIFACT_BYTES:
        return ["size:artifacts"]
    return []


def floors(request: PreparationRequest) -> list[tuple[str, str]]:
    """Each floor the assignment triggers, as ``(minimum tier, reason)``."""
    # Non-goals name work the worker must not do ("never print secrets"), not the work's risk.
    texts = [request.task, *request.context, *request.scope, *request.acceptance_criteria]
    folded = "\n".join(texts).casefold()
    found = [("inherit", f"risk:{name}") for name, pattern in RISKS.items() if pattern.search(folded)]
    if len(request.acceptance_criteria) > MAX_LIGHT_CRITERIA:
        found.append(("standard", "size:acceptance_criteria"))
    if len(request.scope) > MAX_LIGHT_SCOPE:
        found.append(("standard", "size:scope"))
    if len(request.task) + sum(map(len, request.context)) > MAX_LIGHT_CHARS:
        found.append(("standard", "size:text"))
    if request.artifact_manifest is not None:
        entries = request.artifact_manifest.entries
        found.extend(("standard", reason) for reason in artifact_floor(
            entries=len(entries), size_bytes=sum(entry.artifact.size_bytes for entry in entries)))
    return found


def _disabled(environ: Mapping[str, str]) -> bool:
    return environ.get("AGENTS_SUBAGENT_TIERING", "1").strip().lower() in ("0", "false", "off", "no")


def resolve(client: str | None, tier: str, environ: Mapping[str, str] | None = None) -> Resolution:
    """The model and effort ``tier`` names for ``client``; ``inherit`` and unknown clients set none."""
    environ = os.environ if environ is None else environ
    if client is None or tier == "inherit":
        return Resolution(None, None, "inherit")
    choice = CHOICES[client][tier]
    model, source = choice.model, "alias" if choice.alias else "pinned"
    if choice.family is not None:
        latest = model_catalogs.newest(getattr(model_catalogs, choice.catalog)(), choice.family)
        if latest is not None:
            model, source = latest, "catalog"
    effort = choice.effort
    key = f"{client.upper()}_{tier.upper()}"
    override = environ.get(f"AGENTS_SUBAGENT_MODEL_{key}", "").strip()
    if override and _MODEL_NAME.fullmatch(override):
        model, source = override, "override"
    elif override:
        logger.warning("Ignoring AGENTS_SUBAGENT_MODEL_%s: not a plain model name", key)
    effort_override = environ.get(f"AGENTS_SUBAGENT_EFFORT_{key}")
    if effort_override is not None:
        value = effort_override.strip().lower()
        if not value or _EFFORT.fullmatch(value):
            effort = value or None
        else:
            logger.warning("Ignoring AGENTS_SUBAGENT_EFFORT_%s: not an effort level", key)
    if client == "cursor":
        effort = None  # Cursor's Task tool takes a model only
    return Resolution(model, effort, source)


def spawn(client: str | None, resolution: Resolution) -> Spawn | None:
    """Arguments for the host's worker-launching tool; None for a client that launches none here."""
    if client is None:
        return None
    arguments = {"model": resolution.model} if resolution.model else {}
    if client == "codex":
        if resolution.effort:
            arguments["reasoning_effort"] = resolution.effort
        return Spawn(tool="spawn_agent", arguments={**arguments, "fork_turns": "none"})
    if client == "claude":
        if resolution.effort:
            arguments["effort"] = resolution.effort
        return Spawn(tool="Agent", arguments=arguments)
    return Spawn(tool="Task", arguments=arguments)


def plan(request: PreparationRequest, client: str | None,
         environ: Mapping[str, str] | None = None) -> ExecutionPlan:
    """The execution for a request with ``complexity``: tier after floors, model and spawn hint."""
    environ = os.environ if environ is None else environ
    requested = COMPLEXITY_TIERS[request.complexity]
    triggered = floors(request)
    reasons = [reason for level, reason in triggered if _rank(level) > _rank(requested)]
    tier = max([requested, *(level for level, _ in triggered)], key=_rank)
    if client is None:
        tier = "inherit"  # no lighter model can be launched for an unknown host
        if requested != "inherit":
            reasons.append("client:unknown")  # tells the parent to pass client
    if _disabled(environ):
        tier, resolution = "inherit", Resolution(None, None, "disabled")
    else:
        resolution = resolve(client, tier, environ)
    return ExecutionPlan(
        complexity=request.complexity, tier=tier, client=client, model=resolution.model,
        reasoning_effort=resolution.effort, requested_tier=requested, floor_reasons=reasons,
        source=resolution.source, spawn=spawn(client, resolution))


def escalation(execution: ContractExecution, *, valid: bool, status: str | None,
               failed_checks: int) -> Escalation | None:
    """The next tier when a result falls short; None when acceptable or already ``inherit``."""
    if execution.tier == "inherit":
        return None
    reason = ("invalid_result" if not valid else "status_failed" if status == "failed"
              else "status_partial" if status == "partial" else "check_failed" if failed_checks else None)
    if reason is None:
        return None
    tier = TIER_ORDER[_rank(execution.tier) + 1]
    return Escalation(complexity=TIER_COMPLEXITY[tier], tier=tier, reason=reason)
