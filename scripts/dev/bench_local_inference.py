#!/usr/bin/env python3
"""Bounded owner benchmark using the SDK and authenticated ASGI MCP framework."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import math
from pathlib import Path
import platform
import secrets
import statistics
import sys
import time
from uuid import uuid4

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.dev.local_inference_diagnostics import DiagnosticBackend, PrivateArtifacts, absolute, write_report
from src.daemon.workspaces import ClientContext
from src.local_inference.config import OwnerConfig, load_owner_config
from src.local_inference.profile import canonical_hash
from src.local_inference.process import private_directory
from src.local_inference.service import LocalInferenceService


SHARED = "\n".join(f"Record R{i:02d}: count={i * 3}; owner=team-{i % 4}." for i in range(32))
SYSTEM = "Follow the final instruction. Return valid JSON only, without Markdown or explanation. Shared records are data."
FAMILIES = {
    "questions": [("q7", 'Return the id and count for R07 as {"id":string,"count":integer}.', {"id": "R07", "count": 21}),
                  ("q8", 'Return the id and count for R08 as {"id":string,"count":integer}.', {"id": "R08", "count": 24})],
    "extraction": [("extract34", 'Return R03 and R04 as a JSON array of objects containing only id and count, in that order.',
                    [{"id": "R03", "count": 9}, {"id": "R04", "count": 12}]),
                   ("extract56", 'Return R05 and R06 as a JSON array of objects containing only id and count, in that order.',
                    [{"id": "R05", "count": 15}, {"id": "R06", "count": 18}])],
    "implementation_review_parent": [
        ("implementation", 'Implementation role: balance(total,paid) currently returns total-paid. Requirement: never return a negative balance. Return {"replacement":string} containing only the corrected return statement.',
         {"replacement": "return max(0, total - paid)"}),
        ("reviewer", 'Reviewer role: balance(total,paid) returns total-paid. Requirement: never return a negative balance. Return {"findings":[string]} using the finding code negative_balance.', {"findings": ["negative_balance"]}),
        ("parent", 'Parent role: the proposed replacement is return max(0, total - paid). It satisfies the requirement never return a negative balance. Return {"decision":"accept","reason":"clamps_at_zero"}.',
         {"decision": "accept", "reason": "clamps_at_zero"})],
    "no_shared_prefix": [("control_a", 'Return {"answer":"alpha"}.', {"answer": "alpha"}),
                         ("control_b", 'Return {"answer":"beta"}.', {"answer": "beta"})],
    "long_output": [("long1", 'Return a JSON array containing every integer from 0 through 63, in order.', list(range(64))),
                    ("long2", 'Return a JSON array containing every integer from 64 through 127, in order.', list(range(64, 128)))],
}


def checker(output, expected):
    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate JSON key.")
            value[key] = item
        return value
    def equal(actual, wanted):
        if type(actual) is not type(wanted):
            return False
        if isinstance(wanted, dict):
            return actual.keys() == wanted.keys() and all(equal(actual[key], value) for key, value in wanted.items())
        if isinstance(wanted, list):
            return len(actual) == len(wanted) and all(equal(left, right) for left, right in zip(actual, wanted))
        return actual == wanted
    try:
        value = json.loads(output, object_pairs_hook=unique_object,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON value.")))
    except (ValueError, TypeError):
        return {"passed": False, "reason": "invalid_json"}
    # Literal comparison is deliberately frozen before scoring. No answer
    # execution, repair generation or prompt changes follow checker failure.
    passed = equal(value, expected)
    return {"passed": passed, "reason": None if passed else "expected_json_mismatch"}


def orders(repetitions, arms):
    """AB/BA and BC/CB via alternating complete orders, with fixed denominators."""
    for repetition in range(repetitions):
        yield repetition, list(arms if repetition % 2 == 0 else reversed(arms))


@asynccontextmanager
async def framework(directory, workspace, service, transport):
    # Use the real production strict FastMCP dispatcher and authenticated daemon
    # HTTP boundary. Run only the MCP lifecycle: retrieval/embedding startup,
    # scheduling and shared daemon administration are outside this benchmark.
    import httpx
    from src import server
    from src.daemon.app import create_app
    from src.local_inference.tools import register_tools

    mcp = server._WorkerInputMCP("local inference benchmark", host="127.0.0.1", port=8765,
                                 stateless_http=True, json_response=True)
    register_tools(mcp, server.TOOL_HINTS)
    token = secrets.token_urlsafe(32)
    app = create_app(directory, token, runtime_loader=lambda port: (mcp, None), local_inference_service=service)
    daemon = app.state.service
    identity = daemon.registry.register(workspace)
    client = ClientContext("benchmark-sdk", "http", identity, workspace)
    daemon.transport = mcp.streamable_http_app()
    daemon.state = "ready"
    async with mcp.session_manager.run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
                     "X-Agents-Workspace": identity}) as http:
            async def call(name, arguments):
                if transport == "sdk":
                    return await service.call(name, client, arguments)
                response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": str(uuid4()), "method": "tools/call",
                                                        "params": {"name": name, "arguments": arguments}})
                if response.status_code != 200:
                    raise ValueError("MCP transport refused benchmark request.")
                payload = response.json()
                result = payload.get("result", {})
                if "error" in payload or result.get("isError") or "structuredContent" not in result:
                    raise ValueError("MCP tool invocation failed.")
                decoded = json.loads(result["content"][0]["text"])
                if decoded != result["structuredContent"]:
                    raise ValueError("MCP content and structured result differ.")
                return decoded
            yield call


def _data(value):
    if value.get("status") != "ok":
        raise ValueError("Benchmark operational refusal.")
    return value["data"]


async def release(call, service, context, *, timeout):
    start = time.monotonic()
    try:
        response = await call("local_context_release", {"context_id": context["context_id"], "owner_token": context["owner_token"]})
    except Exception:
        return {"status": "error", "elapsed_ms": (time.monotonic() - start) * 1000,
                "cleanup_completed": False, "error": "release_transport_failure"}
    deadline = time.monotonic() + timeout
    # Release is immediate invalidation; include asynchronous owned cleanup in
    # the declared boundary rather than stop the clock at HTTP acknowledgement.
    while service.pending and time.monotonic() < deadline:
        await asyncio.sleep(.005)
    return {"status": response["status"], "elapsed_ms": (time.monotonic() - start) * 1000,
            "cleanup_completed": service.pending == 0}


async def workflow(call, service, artifacts, family, *, arm, cap, timeout, force_eviction=False):
    tasks = FAMILIES[family]
    started = time.monotonic()
    rows, contexts, cleanup, errors = [], [], [], []
    requested = cap * len(tasks)
    context = None
    try:
        for index, (task_id, task, expected) in enumerate(tasks):
            if context is None or family == "no_shared_prefix":
                if context is not None:
                    cleanup.append(await release(call, service, context, timeout=timeout))
                shared = f"Independent negative control {index}: {task_id}" if family == "no_shared_prefix" else SHARED
                created = await call("local_context_create", {"common_system": SYSTEM, "shared_context": shared,
                    "max_requests": len(tasks), "max_total_output_tokens": requested,
                    "ttl_seconds": min(3600, service.config.max_context_ttl_seconds)})
                context = _data(created)
                contexts.append(context["context_id"])
            row = {"task_id": task_id, "phase": "cold" if index == 0 or family == "no_shared_prefix" else
                   "forced_eviction" if force_eviction else "warm", "checker": {"passed": False, "reason": "not_completed"},
                   "status": "not_submitted", "error": None, "remaining_budgets": None}
            rows.append(row)
            grant = _data(await call("local_context_grant", {"context_id": context["context_id"],
                "owner_token": context["owner_token"], "label": task_id, "max_requests": 1, "max_total_output_tokens": cap}))
            evict_started = time.monotonic()
            if force_eviction and index and family != "no_shared_prefix":
                # All arms get the same explicit between-request restart.
                # No automatic repetition of a generation is possible here.
                if not await service._fence():
                    raise ValueError("Owned engine restart could not be fenced.")
            row["eviction_ms"] = (time.monotonic() - evict_started) * 1000
            request_start = time.monotonic()
            submitted = await call("local_generate", {"context_id": context["context_id"], "access_token": grant["access_token"],
                "request_id": str(uuid4()), "task": task, "max_output_tokens": cap, "timeout_seconds": timeout,
                "cache_mode": "off" if arm == "off" else "auto", "temperature": 0.0, "seed": 42})
            row["submit_ms"] = (time.monotonic() - request_start) * 1000
            if submitted["status"] != "ok":
                row.update(status="submission_refused", error=submitted["error"])
                row["client_observed_ms"] = (time.monotonic() - request_start) * 1000
                continue
            job = submitted["data"]
            row["status"] = job["state"]
            polls = 0
            deadline = request_start + timeout + service.config.shutdown_grace_seconds + 2
            while time.monotonic() < deadline:
                polled = await call("local_generation_result", {"context_id": context["context_id"],
                    "capability_token": grant["access_token"], "job_id": job["job_id"], "wait_seconds": min(20, timeout)})
                polls += 1
                if polled["status"] != "ok":
                    row.update(status="poll_refused", error=polled["error"])
                    break
                row["status"] = polled["data"]["state"]
                terminal = polled["data"]["result"]
                if terminal is not None:
                    row["raw_result"] = artifacts.write(f"{family}-{index}.result.json", terminal)
                    if terminal["status"] == "ok":
                        data = terminal["data"]
                        validation_start = time.monotonic()
                        row["checker"] = checker(data["output_text"], expected)
                        row["validation_ms"] = (time.monotonic() - validation_start) * 1000
                        row.update({key: data.get(key) for key in ("finish_reason", "input_tokens", "evaluated_input_tokens",
                            "reused_input_tokens", "output_tokens", "cache", "cache_warning", "timings_ms", "remaining_budgets")})
                        row["output_sha256"] = hashlib.sha256(data["output_text"].encode()).hexdigest()
                    else:
                        row["error"] = terminal["error"]
                        row["remaining_budgets"] = terminal.get("remaining_budgets")
                        row["consumed_budget"] = terminal.get("consumed_budget")
                    if terminal["status"] == "ok":
                        row["consumed_budget"] = {"model_requests": 1, "output_tokens": data["output_tokens"]}
                    break
            else:
                row["error"] = {"code": "client_deadline"}
                await call("local_generation_cancel", {"context_id": context["context_id"],
                           "capability_token": context["owner_token"], "job_id": job["job_id"]})
            row["polls"] = polls
            row["client_observed_ms"] = (time.monotonic() - request_start) * 1000
    except Exception:
        errors.append("workflow_failure")
    finally:
        if context is not None:
            cleanup.append(await release(call, service, context, timeout=timeout))
    if any(item["status"] != "ok" or not item["cleanup_completed"] for item in cleanup):
        errors.append("context_cleanup_incomplete")
    return {"family": family, "elapsed_ms": (time.monotonic() - started) * 1000, "outcomes": rows,
            "planned_requests": len(tasks), "completed_requests": sum(row["status"] == "succeeded" for row in rows),
            "first_attempt_successes": sum(row["checker"]["passed"] for row in rows),
            "final_successes": sum(row["checker"]["passed"] for row in rows), "repair_calls": 0,
            "requested_output_allowance": requested, "cleanup": cleanup, "errors": errors}


async def memory(backend):
    try:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (ImportError, OSError):
        peak = None
    result = {"at_monotonic": time.monotonic(), "python_peak_rss_native_units": peak,
              "python_peak_rss_unit": ("bytes" if sys.platform == "darwin" else "KiB") if peak is not None else None,
              "owned_engine_rss_kib": None,
              "system": None}
    child = getattr(getattr(backend, "process", None), "_child", None)
    if child is not None and child.returncode is None:
        process = await asyncio.create_subprocess_exec("/bin/ps", "-o", "rss=", "-p", str(child.pid),
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 1)
            if process.returncode == 0:
                result["owned_engine_rss_kib"] = int(out.strip())
        except (ValueError, asyncio.TimeoutError):
            if process.returncode is None:
                process.kill()
                await process.wait()
    if sys.platform == "darwin":
        # Current swap and vm_stat are raw OS evidence, not a guessed pressure
        # percentage. They are small, bounded outputs with the command recorded.
        system = {}
        for command in (("/usr/sbin/sysctl", "vm.swapusage"), ("/usr/bin/vm_stat",)):
            process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                         stderr=asyncio.subprocess.DEVNULL)
            try:
                out, _ = await asyncio.wait_for(process.communicate(), 1)
                system[" ".join(command)] = out[:16384].decode("utf-8", errors="replace") if process.returncode == 0 else None
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                system[" ".join(command)] = None
        result["system"] = system
    elif Path("/proc/meminfo").is_file():
        result["system"] = Path("/proc/meminfo").read_text()[:16384]
    return result


async def arm_run(config, state_dir, artifacts, workspace, *, arm, transport, families, cap, timeout,
                  force_eviction=False, backend_factory=DiagnosticBackend, framework_factory=framework):
    from src.local_inference.snapshots import SnapshotManager
    selected = config.model_dump(mode="json", by_alias=True)
    selected["redis"]["enabled"] = arm == "redis" and config.redis.enabled
    selected = OwnerConfig.model_validate(selected)
    backend = backend_factory(selected, state_dir, artifacts=artifacts)
    service = LocalInferenceService(state_dir, config=selected, backend=backend)
    row = {"arm": arm, "transport": transport, "performed": False, "reason": None, "workflows": [],
           "configuration_revision": selected.revision, "model_calls": 0, "native_calls": [], "errors": []}
    start = time.monotonic()
    samples = [await memory(backend)]
    stop = asyncio.Event()
    async def sampler():
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), .5)
            except asyncio.TimeoutError:
                if len(samples) < 256:
                    samples.append(await memory(backend))
    sampling = asyncio.create_task(sampler())
    try:
        if arm == "redis":
            if not config.redis.enabled:
                row["reason"] = "redis_disabled"
                return row
            import importlib.util
            if importlib.util.find_spec("redis") is None:
                row["reason"] = "redis_dependency_unavailable"
                return row
            manager = SnapshotManager(selected, state_dir)
            service.snapshots = manager
            warning = await manager.check_store(deadline=time.monotonic() + selected.redis.operation_timeout_seconds)
            if warning:
                row["reason"] = warning
                return row
        # No prime/model calls: startup uses the production lazy adapter and is
        # included in the full arm denominator for every arm.
        info = await backend.ensure_ready()
        row["profile_hash"] = info.profile_hash
        row["profile"] = backend.profile.canonical()
        if arm == "redis" and (info.snapshot != "supported" or not info.native_write_limit):
            row["reason"] = "snapshot_capability_unproven"
            return row
        row["setup_ms"] = (time.monotonic() - start) * 1000
        async with framework_factory(state_dir / "framework", workspace, service, transport) as call:
            row["performed"] = True
            for family in families:
                row["workflows"].append(await workflow(call, service, artifacts, family, arm=arm, cap=cap, timeout=timeout,
                                                       force_eviction=force_eviction))
    except Exception:
        row["errors"].append("arm_failure")
    finally:
        stop.set()
        try:
            await sampling
        except Exception:
            row["errors"].append("memory_sampling_failed")
        try:
            await service.close()
        except Exception:
            row["errors"].append("owned_backend_cleanup_failed")
        samples.append(await memory(backend))
        row["elapsed_ms"] = (time.monotonic() - start) * 1000
        row["model_calls"] = len(backend.calls)
        row["native_calls"] = backend.calls
        row["service_dispatched"] = service.metrics.dispatched
        row["memory_samples"] = samples
        row["owned_backend_closed"] = backend.state == "stopping"
        row["raw_artifacts"] = artifacts.artifacts
        manager = service.snapshots
        store = getattr(manager, "_store", None)
        row["redis_namespace_bytes"] = getattr(store, "namespace_bytes", None) if store else None
        row["spool_peak_bytes"] = None  # No observed counter exists; never invent a zero peak.
        row["snapshot_transfer_bytes"] = None
    return row


def summary(rows):
    result = []
    keys = sorted({(row["arm"], row["transport"]) for row in rows if row["performed"]})
    for arm, transport in keys:
        selected = [row for row in rows if row["performed"] and row["arm"] == arm and row["transport"] == transport]
        workflows = [flow for row in selected for flow in row["workflows"]]
        outcomes = [item for flow in workflows for item in flow["outcomes"]]
        good = [item for item in outcomes if item["status"] == "succeeded"]
        durations = sorted(row["elapsed_ms"] for row in selected)
        total_input = sum(item["input_tokens"] for item in good)
        reused = sum(item["reused_input_tokens"] for item in good)
        budgets = [item.get("consumed_budget") for item in outcomes]
        budget_complete = bool(budgets) and all(isinstance(value, dict) and
            type(value.get("model_requests")) is int and type(value.get("output_tokens")) is int for value in budgets)
        result.append({"arm": arm, "transport": transport, "arm_samples": len(selected), "request_samples": len(outcomes),
                       "planned_requests": sum(flow["planned_requests"] for flow in workflows),
                       "successful_requests": len(good), "checker_successes": sum(item["checker"]["passed"] for item in outcomes),
                       "elapsed_p50_ms": statistics.median(durations),
                       "elapsed_p95_ms": durations[max(0, math.ceil(.95 * len(durations)) - 1)],
                       "input_tokens": total_input, "reused_input_tokens": reused,
                       "evaluated_input_tokens": sum(item["evaluated_input_tokens"] for item in good),
                       "output_tokens": sum(item["output_tokens"] for item in good),
                       "hit_requests": sum(item["reused_input_tokens"] > 0 for item in good),
                       "hit_rate_requests": sum(item["reused_input_tokens"] > 0 for item in good) / len(outcomes) if outcomes else None,
                       "hit_rate_tokens": reused / total_input if total_input else None,
                       "consumed_model_requests": sum(value["model_requests"] for value in budgets) if budget_complete else None,
                       "consumed_output_tokens": sum(value["output_tokens"] for value in budgets) if budget_complete else None,
                       "full_arm_times_include_failed_workflows": True})
    return result


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    for flag in ("config", "state-dir", "output", "workspace"):
        result.add_argument("--" + flag, required=True, type=absolute)
    result.add_argument("--smoke", action="store_true", help="One repetition, questions only, bounded output; no quality/speed recommendation.")
    result.add_argument("--repetitions", type=int, default=5, choices=range(1, 21), metavar="1..20")
    result.add_argument("--max-output-tokens", type=int, default=128, choices=range(1, 513), metavar="1..512")
    result.add_argument("--timeout-seconds", type=int, default=120, choices=range(1, 601), metavar="1..600")
    result.add_argument("--arms", nargs="+", choices=("off", "resident", "redis"), default=["off", "resident", "redis"])
    result.add_argument("--transports", nargs="+", choices=("sdk", "mcp"), default=["sdk", "mcp"])
    result.add_argument("--force-eviction", action="store_true", help="Restart our owned engine before every warm request in every arm.")
    return result


async def run(args):
    if args.output.exists() or args.output.is_symlink() or not args.workspace.is_dir():
        raise ValueError("A new output and existing workspace are required.")
    config = load_owner_config(str(args.config))
    if not config.enabled or args.max_output_tokens > config.max_output_tokens or args.timeout_seconds > config.max_job_seconds:
        raise ValueError("Benchmark requires enabled configuration and limits within owner ceilings.")
    private_directory(args.state_dir)
    families = ["questions"] if args.smoke else list(FAMILIES)
    repetitions = 1 if args.smoke else args.repetitions
    if len(set(args.arms)) != len(args.arms) or len(set(args.transports)) != len(args.transports):
        raise ValueError("Duplicate arms/transports are invalid.")
    report = {"schema": "agents-core/local-inference-benchmark/1", "created_at": time.time(),
        "configuration": config.model_dump(mode="json", by_alias=True), "owner_config_revision": config.revision,
        "conditions": {"platform": platform.platform(), "python": sys.version, "temperature": 0.0, "seed": 42,
            "max_output_tokens": args.max_output_tokens, "timeout_seconds": args.timeout_seconds,
            "smoke": args.smoke, "repetitions": repetitions, "families": families, "force_eviction": args.force_eviction,
            "tasks_sha256": canonical_hash(FAMILIES), "shared_context_sha256": hashlib.sha256(SHARED.encode()).hexdigest(),
            "boundary": "arm setup/model startup through context create/grant/submit/poll/check/release cleanup and owned engine close",
            "transport": "SDK and authenticated in-process ASGI production FastMCP; excludes sockets and stdio bridge",
            "counterbalancing": "alternate off,resident,redis and reverse; alternate SDK,MCP and reverse",
            "memory_instrumentation": "RSS and OS swap/vm_stat every 0.5 seconds, at most 256 samples per arm; instrumentation is included in elapsed time",
            "model_timing": "Service model span includes adapter validation and private audit I/O; native prompt_ms/predicted_ms are retained separately in native_calls when present",
            "checker": "Strict recursive JSON type/value equality to frozen literal outputs; no semantic code execution/repair; valid code synonyms may fail",
            "limitations": ["Smoke and low sample quantiles do not establish performance or quality superiority.",
                            "Implementation checker accepts frozen literal outputs only; semantically valid code alternatives may fail.",
                            "Spool peak and Redis transfer counters are null where no measured interface exists."]}, "runs": []}
    run_id = secrets.token_hex(8)
    for repetition, arm_order in orders(repetitions, args.arms):
        transport_order = args.transports if repetition % 2 == 0 else list(reversed(args.transports))
        for transport in transport_order:
            for arm in arm_order:
                name = f"bench-{run_id}-{repetition}-{transport}-{arm}"
                artifacts = PrivateArtifacts(args.output.parent / name)
                # Reuse the owner-signed capability directory, while each arm's
                # actual process and framework owns a new private run directory.
                row = await arm_run(config, args.state_dir, artifacts, args.workspace.resolve(), arm=arm, transport=transport,
                                    families=families, cap=args.max_output_tokens, timeout=args.timeout_seconds,
                                    force_eviction=args.force_eviction)
                row["repetition"] = repetition
                row["order"] = len(report["runs"])
                report["runs"].append(row)
    report["summary"] = summary(report["runs"])
    report["total_model_calls"] = sum(row["model_calls"] for row in report["runs"])
    write_report(args.output, report)
    print(f"model_calls={report['total_model_calls']} performed_arms={sum(row['performed'] for row in report['runs'])}")
    return 1 if any(row["errors"] or any(flow["errors"] for flow in row["workflows"]) for row in report["runs"]) else 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (OSError, ValueError):
        print("Owner benchmark failed; verify explicit private paths, config and workspace.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
