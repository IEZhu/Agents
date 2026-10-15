"""Thin opt-in MCP tools using the daemon's request-scoped broker.

No singleton, model initialization, logging or tracing occurs here. Validation of
original argument dictionaries is also used by the server's strict dispatcher.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from pydantic import ValidationError

from src.daemon.workspaces import resolve_client_context
from src.schemas import tool_args as ta

from .models import LocalInferenceError
from .service import REQUEST_MODELS

TOOL_NAMES = ("local_inference_status", "local_context_create", "local_context_grant",
              "local_context_revoke", "local_generate", "local_generation_result",
              "local_generation_cancel", "local_context_release")
INVALID_ARGUMENTS = "Invalid local inference arguments."


def validate_arguments(name: str, arguments: dict):
    """Reject untouched inputs before FastMCP coercion or unknown-field removal."""
    if type(arguments) is not dict:
        raise ValueError(INVALID_ARGUMENTS)
    if name == "local_inference_status":
        if arguments:
            raise ValueError(INVALID_ARGUMENTS)
        return
    try:
        REQUEST_MODELS[name].model_validate(arguments)
    except ValidationError:
        # Pydantic diagnostics can echo task/token values. Never expose them.
        raise ValueError(INVALID_ARGUMENTS) from None


async def _call(name: str, ctx: Context | None, arguments: dict) -> dict[str, Any]:
    validate_arguments(name, arguments)
    try:
        try:
            request = ctx.request_context.request
        except (AttributeError, ValueError):
            request = None
        if request is None:
            raise LocalInferenceError("transport_unsupported", "Local inference requires the shared HTTP daemon.")
        client = await resolve_client_context(ctx, allow_install_fallback=False)
        service = getattr(request.state, "local_inference_service", None)
        if service is None:
            raise LocalInferenceError("backend_unavailable", "The local inference broker is unavailable.")
        return await service.call(name, client, arguments)
    except LocalInferenceError as error:
        return error.envelope()


def register_tools(mcp, hints: dict):
    """Register the same functions on production and model-free test runtimes."""
    for name in TOOL_NAMES:
        mcp.add_tool(globals()[name], **hints[name])


async def local_inference_status(ctx: Context | None = None) -> dict[str, Any]:
    """Read enabled state, readiness, capabilities, job counts and owner hard limits.

    Performs no model start or network call; reveals no host paths or secrets.
    """
    return await _call("local_inference_status", ctx, {})


async def local_context_create(
    common_system: ta.unparsed_arg("string", "Required UTF-8 common system instructions; empty allowed. Context total capped by owner, default 256 KiB."),
    shared_context: ta.unparsed_arg("string", "Required UTF-8 shared data. Shared text plus captured files must contain data."),
    source_paths: ta.unparsed_arg("array", "At most 32 unique regular UTF-8 files relative to source_root_id; default empty. No absolute paths, traversal or symlinks.", items={"type": "string"}) = [],
    source_root_id: ta.unparsed_arg("string", "Optional owner-configured root ID, null by default; required with source_paths. No raw root path override.") = None,
    ttl_seconds: ta.unparsed_arg("integer", "Context lifetime 30..3600 seconds, default 900, further capped by owner.") = 900,
    max_requests: ta.unparsed_arg("integer", "Context model-call budget 1..100, default 10, capped by owner.") = 10,
    max_total_output_tokens: ta.unparsed_arg("integer", "Context aggregate output budget 1..65536, default 4096, capped by owner.") = 4096,
    artifact_manifest: ta.unparsed_arg("object", "Optional pinned active artifact manifest. The broker hydrates its exact selected artifacts before generation, checks byte/token budgets and rechecks access/integrity before dispatch, publication and result reads.") = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Capture an immutable workspace-bound context without starting a model.

    Returns owner/access bearer tokens once; keep them outside prompts and logs.
    """
    return await _call("local_context_create", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_context_grant(
    context_id: ta.unparsed_arg("string", "Required context ID returned by create."),
    owner_token: ta.unparsed_arg("string", "Required separate context owner capability; never authorize by label."),
    label: ta.unparsed_arg("string", "Required diagnostic label 1..80 characters; grants no authority."),
    max_requests: ta.unparsed_arg("integer", "Required grant request cap 1..100, at most remaining context budget."),
    max_total_output_tokens: ta.unparsed_arg("integer", "Required grant output cap 1..65536, at most remaining context budget."),
    ttl_seconds: ta.unparsed_arg("integer", "Optional lifetime 1..3600 seconds; null means remaining context lifetime and cannot exceed it.") = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Create a scoped child access capability; grant caps do not reserve shared credits."""
    return await _call("local_context_grant", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_context_revoke(
    context_id: ta.unparsed_arg("string", "Required context ID."),
    owner_token: ta.unparsed_arg("string", "Required separate owner capability."),
    grant_id: ta.unparsed_arg("string", "Required child grant ID; owner access grant cannot be revoked."),
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Revoke a grant, suppress publication and fence its active job; repeat safely."""
    return await _call("local_context_revoke", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_generate(
    context_id: ta.unparsed_arg("string", "Required context ID."),
    access_token: ta.unparsed_arg("string", "Required scoped access capability; keep outside task text and logs."),
    request_id: ta.unparsed_arg("string", "Required canonical lowercase UUID idempotency key. Same ID/grant/arguments returns the original job."),
    task: ta.unparsed_arg("string", "Required nonempty UTF-8 task, at most 64 KiB and owner limit. JSON-looking text remains exact text."),
    max_output_tokens: ta.unparsed_arg("integer", "Output allowance 1..2048, default 512, capped by owner/context/grant and effective model context.") = 512,
    timeout_seconds: ta.unparsed_arg("integer", "Total job deadline 1..600 seconds, default 120, capped by owner/context/grant expiry.") = 120,
    cache_mode: ta.unparsed_arg("string", "auto(default) or off. off disables native reuse and snapshot I/O.") = "auto",
    temperature: ta.unparsed_arg("number", "Finite sampling temperature 0..2, default 0.") = 0.0,
    seed: ta.unparsed_arg("integer", "Integer seed 0..2147483647, default 42.") = 42,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Submit one bounded textual completion and promptly return its job ID.

    Poll local_generation_result; this tool never executes output or spawns agents.
    """
    return await _call("local_generate", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_generation_result(
    context_id: ta.unparsed_arg("string", "Required context ID."),
    capability_token: ta.unparsed_arg("string", "Required submitting access capability or separate context owner capability."),
    job_id: ta.unparsed_arg("string", "Required accepted job ID."),
    wait_seconds: ta.unparsed_arg("integer", "Poll wait 0..20 seconds, default 0; never changes job deadline or dispatches work.") = 0,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Read retained job state/output; disconnecting a poll never cancels generation."""
    return await _call("local_generation_result", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_generation_cancel(
    context_id: ta.unparsed_arg("string", "Required context ID."),
    capability_token: ta.unparsed_arg("string", "Required submitting access capability or separate context owner capability."),
    job_id: ta.unparsed_arg("string", "Required accepted job ID."),
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Explicitly cancel a job for all pollers; active cleanup fences/reaps before terminal cancellation."""
    return await _call("local_generation_cancel", ctx, {key: value for key, value in locals().items() if key != "ctx"})


async def local_context_release(
    context_id: ta.unparsed_arg("string", "Required context ID."),
    owner_token: ta.unparsed_arg("string", "Required separate context owner capability."),
    ctx: Context | None = None,
) -> dict[str, Any]:
    """Invalidate context/grants and cancel work; bounded tombstones permit repeated release."""
    return await _call("local_context_release", ctx, {key: value for key, value in locals().items() if key != "ctx"})
