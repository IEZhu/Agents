"""Evaluate real resumed CLI dialogues, retaining prompts and actual MCP traces.

Labels are inspected only after a completed turn; they never enter model input.
Use the same dataset for baseline and candidate, and three independent repeats
per client/model for acceptance. Report failures individually, never by average.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from pydantic import ValidationError

from src.schemas.protocol import PersonaDescriptor

ROOT = Path(__file__).resolve().parents[2]
SELECTION_TOOLS = {"route_and_load", "get_agent_context", "refresh_persona_context", "list_agents", "load_implants"}
ALLOWED_TOOLS = sorted(SELECTION_TOOLS | {"log_interaction", "read_history"})


def events_from(text: str) -> list[dict]:
    events = []
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def payload_from(value) -> dict:
    """Unwrap FastMCP structured/text results without selecting a guessed agent."""
    if isinstance(value, str):
        try:
            return payload_from(json.loads(value))
        except ValueError:
            return {}
    if isinstance(value, dict):
        if "status" in value or ("request_id" in value and "history" in value):
            return value
        for key in ("structuredContent", "text", "content"):
            payload = payload_from(value.get(key))
            if payload:
                return payload
    if isinstance(value, (list, tuple)):
        for part in value:
            payload = payload_from(part)
            if payload:
                return payload
    return {}


def client_output(events: list[dict]) -> dict:
    texts, models, errors, attempts, usage = [], set(), [], [], []
    session = None
    for event in events:
        session = event.get("thread_id") or event.get("session_id") or session
        if event.get("type") == "error" or event.get("is_error"):
            errors.append(event)
        item = event.get("item", {})
        if event.get("type") == "item.completed":
            if item.get("type") == "agent_message":
                texts.append(item.get("text", ""))
            elif item.get("type") == "mcp_tool_call":
                attempts.append(item.get("tool"))
                if item.get("error"):
                    errors.append(item["error"])
        if event.get("type") == "assistant":
            message = event.get("message", {})
            if message.get("model"):
                models.add(message["model"])
            for part in message.get("content", []):
                if part.get("type") == "text":
                    texts.append(part.get("text", ""))
                elif part.get("type") == "tool_use":
                    attempts.append(part.get("name", "").removeprefix("mcp__Agents_Core__"))
        if event.get("model"):
            models.add(event["model"])
        if event.get("type") in {"turn.completed", "result"} and event.get("usage"):
            usage.append(event["usage"])
        if event.get("permission_denials"):
            errors.append({"permission_denials": event["permission_denials"]})
    finals = [event.get("result", "") for event in events if event.get("type") == "result"]
    final_answer = finals[-1] if finals else texts[-1] if texts else ""
    return {"answer": final_answer, "visible_messages": texts, "models": sorted(models), "session": session,
            "client_errors": errors, "attempted_tools": attempts, "usage": usage}


def assess_turn(turn: dict, trace: list[dict], output: dict, active: dict | None, protocol_version: int) -> tuple[dict, dict | None]:
    """Judge successful server activations, not role names echoed in model prose."""
    failures = []
    previous = active
    selection_calls = [call for call in trace if call["tool"] in SELECTION_TOOLS]
    attempted_selection = [name for name in output["attempted_tools"] if name in SELECTION_TOOLS]
    successful_loads = []
    valid_refresh = False
    for call in trace:
        if call.get("error"):
            failures.append("server_tool_error")
            continue
        payload = payload_from(call.get("result"))
        if payload.get("status") == "ERROR":
            failures.append("server_error_result")
        if call["tool"] not in {"route_and_load", "get_agent_context", "refresh_persona_context"}:
            continue
        if payload.get("status") not in {"SUCCESS", "SUCCESS_SAMPLED", "NO_CHANGE"}:
            continue
        if protocol_version == 2:
            if payload["status"] == "SUCCESS_SAMPLED":
                failures.append("sampling_in_v2")
                continue
            failures_before_payload = len(failures)
            persona = payload.get("persona")
            if payload.get("protocol_version") != 2 or not isinstance(persona, dict):
                failures.append("not_a_v2_activation")
                continue
            if payload["status"] == "SUCCESS":
                expected_replaced = (active or {}).get("activation_id")
                if payload.get("replaces_activation_id") != expected_replaced:
                    failures.append("activation_chain_mismatch")
                if (not all(isinstance(payload.get(key), str) for key in
                            ("persona_block", "rules_block", "skills_block", "implants_block"))
                        or not payload["persona_block"].strip()
                        or not isinstance(payload.get("footer"), str) or not payload["footer"].strip()):
                    failures.append("incomplete_bundle")
                try:
                    PersonaDescriptor.model_validate(persona, strict=True)
                except ValidationError:
                    failures.append("incomplete_descriptor")
                if len(failures) == failures_before_payload:
                    active = {**persona, "footer": payload["footer"]}
                    successful_loads.append(call)
            elif payload["status"] == "NO_CHANGE":
                descriptor = {key: value for key, value in (active or {}).items() if key != "footer"}
                if persona != descriptor:
                    failures.append("no_change_changed_activation")
            if call["tool"] == "refresh_persona_context" and len(failures) == failures_before_payload:
                valid_refresh = True
        elif payload.get("agent"):
            active = {"agent": payload["agent"], "context_hash": payload.get("context_hash")}
            if payload["status"] != "NO_CHANGE":
                successful_loads.append(call)
    expected = turn["expected"]
    if expected == "keep":
        if selection_calls or attempted_selection:
            failures.append("unnecessary_selection_or_enrichment")
        if active is None or active != previous:
            failures.append("keep_without_same_active_persona")
    elif expected in {"load", "switch"}:
        if not successful_loads:
            failures.append("missing_successful_load")
        if expected == "switch" and previous and active and previous["agent"] == active["agent"]:
            failures.append("specialization_did_not_change")
    elif expected == "refresh":
        if protocol_version == 2 and not valid_refresh:
            failures.append("missing_successful_refresh")
        if not selection_calls or any(call["tool"] != "refresh_persona_context" for call in selection_calls):
            failures.append("refresh_used_selection")
        if not previous or not active or previous["agent"] != active["agent"]:
            failures.append("refresh_changed_agent")
    elif expected == "restore":
        if not previous or not active or previous["agent"] != active["agent"]:
            failures.append("restore_changed_agent")
        if not successful_loads or any(call["tool"] != "get_agent_context" or not call["arguments"].get("force_reload") for call in selection_calls):
            failures.append("restore_used_selection")
    if expected == "load" and not turn.get("direct") and protocol_version == 2:
        if not selection_calls or selection_calls[0]["tool"] != "route_and_load":
            failures.append("initial_selection_skipped_routing")
    if turn.get("direct") and protocol_version == 2 and any(call["tool"] == "route_and_load" for call in selection_calls):
        failures.append("explicit_role_was_routed")
    if not active or active.get("agent") != turn["agent"]:
        failures.append("wrong_active_agent")
    answer = output["answer"]
    if not answer:
        failures.append("missing_answer")
    if any(f.casefold() not in answer.casefold() for f in turn.get("facts", [])):
        failures.append("missing_conversation_fact")
    footer_agents = re.findall(r"\*\*Agent\*\*:\s*([\w-]+)", answer)
    if not footer_agents or footer_agents[-1] != turn["agent"]:
        failures.append("wrong_footer_agent")
    if protocol_version == 2 and active and active.get("footer") and active["footer"] not in answer:
        failures.append("footer_differs_from_bundle")
    logs = [call for call in trace if call["tool"] == "log_interaction" and not call.get("error")]
    for call in logs:
        if call["arguments"].get("query") != turn["query"]:
            failures.append("log_query_mismatch")
    if protocol_version == 2:
        expected_action = "switch" if expected == "load" else expected
        if not logs:
            failures.append("missing_attribution_log")
        for call in logs:
            logged = call["arguments"]
            descriptor = {key: value for key, value in (active or {}).items() if key != "footer"}
            if logged.get("agent_name") != turn["agent"] or logged.get("persona") != descriptor:
                failures.append("log_descriptor_mismatch")
            if logged.get("persona_action") != expected_action:
                failures.append("log_action_mismatch")
            if payload_from(call.get("result")).get("history", {}).get("status") not in {"recorded", "duplicate"}:
                failures.append("log_not_recorded")
            if logged.get("response_content", "").strip() != answer.strip():
                failures.append("log_response_differs_from_final")
    if output["client_errors"]:
        failures.append("client_error")
    switched = bool(previous and active and previous["agent"] != active["agent"])
    return {"observed_switch": switched, "passed": not failures, "failures": sorted(set(failures)), "selection_calls": len(selection_calls),
            "attempted_selection_calls": len(attempted_selection), "active_persona": active,
            "tool_result_bytes": sum(call.get("result_bytes", 0) for call in trace)}, active


def require_process_group_support() -> None:
    """Reject Windows until the evaluator supports and tests process-tree cleanup."""
    if sys.platform == "win32":
        raise NotImplementedError(
            "Persona dialogue evaluations do not support Windows process-tree cleanup. "
            "Run the evaluator on Linux or macOS."
        )


def run_process(cmd: list[str], query: str, workspace: Path, timeout: int) -> tuple[int, str, str, bool]:
    """Kill the entire evaluation process group on timeout, including its MCP."""
    require_process_group_support()
    with subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, encoding="utf-8", cwd=workspace, start_new_session=True) as process:
        try:
            stdout, stderr = process.communicate(query, timeout=timeout)
            return process.returncode, stdout, stderr, False
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            return process.returncode, stdout, stderr, True


def codex_session_models(session: str | None) -> list[str]:
    """Codex exec JSON omits model IDs; read only this run's saved turn metadata."""
    if not session:
        return []
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    models = set()
    for path in (codex_dir / "sessions").rglob(f"*{session}*.jsonl"):
        for event in events_from(path.read_text(encoding="utf-8")):
            if event.get("type") == "turn_context" and event.get("payload", {}).get("model"):
                models.add(event["payload"]["model"])
    return sorted(models)


def turn_input(turn: dict, active: dict | None) -> tuple[str, bool, dict | None]:
    """Simulated compaction starts a fresh CLI session with only a factual summary.

    The fixture specifies retained information, never the tool/action to choose.
    """
    reset = turn.get("context_reset")
    if not reset:
        return turn["query"], False, active
    if reset not in {"known_persona", "unknown_persona"}:
        raise ValueError(f"Unknown context reset: {reset}")
    summary = "Conversation summary after compaction:\n" + turn["summary"]
    if reset == "known_persona":
        if not active:
            raise ValueError("Cannot retain a persona that never loaded")
        descriptor = {key: value for key, value in active.items() if key != "footer"}
        summary += "\nRetained active persona descriptor: " + json.dumps(descriptor, ensure_ascii=False)
    else:
        active = None
        summary += "\nNo active persona name or descriptor was retained."
    summary += "\nPersona instruction blocks were not retained.\n\nCurrent request:\n" + turn["query"]
    return summary, True, active


def isolate_codex_global_instructions(command: list[str]) -> list[str]:
    """macOS test-fixture isolation: deny only inherited global instruction files.

    Adds a read restriction. It changes no user file, auth/home configuration, or
    existing Codex sandbox/approval setting. Project guidance is supplied verbatim
    through developer_instructions with project_doc_max_bytes=0.
    """
    if sys.platform != "darwin":
        raise ValueError("Global instruction read isolation currently requires macOS sandbox-exec")
    codex_dir = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    rules = "".join("(literal " + json.dumps(str(codex_dir / name)) + ")" for name in ("AGENTS.md", "AGENTS.override.md"))
    return ["sandbox-exec", "-p", "(version 1)(allow default)(deny file-read* " + rules + ")", *command]


def project_interpreter(root: Path) -> str:
    """Require the project's virtualenv instead of guessing a dependency runtime."""
    relative = "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
    python = root / ".venv" / relative
    if not python.is_file():
        raise FileNotFoundError(
            f"Project virtualenv interpreter not found: {python}. "
            "Create the project virtualenv and install its dependencies before running evaluations."
        )
    if sys.platform != "win32" and not os.access(python, os.X_OK):
        raise PermissionError(f"Project virtualenv interpreter is not executable: {python}")
    return str(python)


def run_case(client: str, case: dict, workspace: Path, protocol: str, timeout: int,
             source_root: Path = ROOT, protocol_version: int = 2, seed_data: Path = ROOT / "data", isolate_codex: bool = False) -> dict:
    require_process_group_support()
    python = project_interpreter(ROOT)
    workspace.mkdir(parents=True)  # Do not silently overwrite a prior experiment.
    for name in ("AGENTS.md", "CLAUDE.md"):
        (workspace / name).write_text(protocol, encoding="utf-8")
    server_args = [str(ROOT / "evals/runners/persona_server.py"), "--workspace", str(workspace),
                   "--source-root", str(source_root), "--seed-data", str(seed_data)]
    config = {"mcpServers": {"Agents_Core": {"command": python, "args": server_args}}}
    config_path = workspace / "mcp.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    session, active, results, models = None, None, [], set()
    for index, turn in enumerate(case["turns"]):
        query, reset, active = turn_input(turn, active)
        if reset:
            session = None
        trace_path = workspace / "trace.jsonl"
        before = len(trace_path.read_text(encoding="utf-8").splitlines()) if trace_path.exists() else 0
        if client == "codex":
            cmd = ["codex", "exec", "--ignore-user-config", "--json", "--skip-git-repo-check",
                   "-c", 'sandbox_mode="read-only"', "-c", "agents.enabled=false",
                   "-c", "project_doc_max_bytes=0", "-c", f"developer_instructions={json.dumps(protocol)}",
                   "-c", f"mcp_servers.Agents_Core.command={json.dumps(python)}",
                   "-c", f"mcp_servers.Agents_Core.args={json.dumps(server_args)}",
                   "-c", f"mcp_servers.Agents_Core.enabled_tools={json.dumps(ALLOWED_TOOLS)}",
                   "-c", "mcp_servers.Agents_Core.startup_timeout_sec=120",
                   "-c", "mcp_servers.Agents_Core.required=true",
                   "-c", 'mcp_servers.Agents_Core.default_tools_approval_mode="approve"']
            cmd += ["resume", session, "-"] if session else ["-"]
            if isolate_codex:
                cmd = isolate_codex_global_instructions(cmd)
        else:
            cmd = ["claude", "--bare", "--print", "--verbose", "--output-format", "stream-json",
                   "--strict-mcp-config", "--mcp-config", str(config_path), "--tools", "",
                   "--allowedTools", *[f"mcp__Agents_Core__{name}" for name in ALLOWED_TOOLS],
                   "--setting-sources", "", "--disable-slash-commands", "--append-system-prompt", protocol]
            if session:
                cmd += ["--resume", session]
        start = time.monotonic()
        code, stdout, stderr, timeout_hit = run_process(cmd, query, workspace, timeout)
        (workspace / f"turn-{index}.jsonl").write_text(stdout, encoding="utf-8")
        (workspace / f"turn-{index}.stderr").write_text(stderr, encoding="utf-8")
        output = client_output(events_from(stdout))
        session = output.pop("session") or session
        if client == "codex":
            models.update(codex_session_models(session))
        models.update(output.pop("models"))
        trace = events_from("\n".join(trace_path.read_text(encoding="utf-8").splitlines()[before:])) if trace_path.exists() else []
        verdict, active = assess_turn(turn, trace, output, active, protocol_version)
        if timeout_hit or code:
            verdict["passed"] = False
            verdict["failures"].append("client_timeout" if timeout_hit else "client_exit_error")
        results.append({"turn": index, "query": turn["query"], "model_input": query, "expected": turn["expected"], **verdict,
                        **output, "trace": trace, "exit_code": code,
                        "elapsed_ms": round(1000 * (time.monotonic() - start))})
        if code or not session or output["client_errors"]:
            break
    if client == "codex":
        models.update(codex_session_models(session))
    return {"id": case["id"], "client": client, "models": sorted(models), "session": session, "turns": results,
            "complete": len(results) == len(case["turns"]),
            "passed": len(results) == len(case["turns"]) and all(t["passed"] for t in results)}


def summarize(results: list[dict]) -> dict:
    turns = [turn for result in results for turn in result["turns"]]
    expected_switches = [turn for turn in turns if turn["expected"] == "switch"]
    actual_switches = [turn for turn in turns if turn["observed_switch"]]
    true_switches = sum(turn["expected"] == "switch" and "wrong_active_agent" not in turn["failures"] for turn in actual_switches)
    return {"switch_precision": true_switches / len(actual_switches) if actual_switches else None,
            "switch_recall": true_switches / len(expected_switches) if expected_switches else None,
            "false_switches": sum(turn["expected"] != "switch" for turn in actual_switches),
            "unnecessary_refreshes": sum(call["tool"] == "refresh_persona_context" for turn in turns if turn["expected"] != "refresh" for call in turn["trace"]),
            "cases": len(results), "passed_cases": sum(r["passed"] for r in results), "turns": len(turns),
            "failed_turns": sum(not t["passed"] for t in turns),
            "unnecessary_keep_calls": sum(t["attempted_selection_calls"] for t in turns if t["expected"] == "keep"),
            "missed_switches": sum("missing_successful_load" in t["failures"] or "wrong_active_agent" in t["failures"] for t in expected_switches),
            "tool_result_bytes": sum(t["tool_result_bytes"] for t in turns),
            "elapsed_turn_ms": sum(t["elapsed_ms"] for t in turns)}


def reassess_report(report: dict, cases: list[dict]) -> dict:
    """Regrade retained traces after an audited label/scorer fix; never rerun a model.

    Preserve original verdicts and run hashes so a correction remains reviewable.
    """
    revised = json.loads(json.dumps(report))
    by_id = {case["id"]: case for case in cases}
    for case_result in revised["results"]:
        active = None
        for item in case_result["turns"]:
            turn = by_id[case_result["id"]]["turns"][item["turn"]]
            if turn.get("context_reset") == "unknown_persona":
                active = None
            original_failures = item["failures"]
            verdict, active = assess_turn(turn, item["trace"], item, active, report["protocol_version"])
            process_failures = [failure for failure in original_failures if failure in {"client_timeout", "client_exit_error"}]
            verdict["failures"] = sorted(set(verdict["failures"] + process_failures))
            verdict["passed"] = not verdict["failures"]
            item["original_failures"] = original_failures
            item.update(verdict)
        case_result["passed"] = case_result["complete"] and all(turn["passed"] for turn in case_result["turns"])
    revised["summary"] = summarize(revised["results"])
    revised["scoring_labels_sha256"] = hashlib.sha256(json.dumps(cases, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    revised["scorer_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return revised


def tree_revision(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            digest.update(str(path.relative_to(root)).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--client", choices=["codex", "claude"], required=True)
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--protocol", type=Path, default=ROOT / "scripts/templates/routing-protocol-core.md")
    p.add_argument("--protocol-version", type=int, choices=[1, 2], default=2)
    p.add_argument("--source-root", type=Path, default=ROOT, help="git-archive directory for baseline runtime")
    p.add_argument("--codex-isolate-global-instructions", action="store_true", help="macOS: deny child reads of global AGENTS files; does not modify them")
    p.add_argument("--seed-data", type=Path, required=True, help="frozen data seed; same directory for baseline and candidate")
    p.add_argument("--dataset", type=Path, default=ROOT / "evals/datasets/persona_dialogues.jsonl")
    p.add_argument("--case")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument("--jobs", type=int, default=1)
    p.add_argument("--timeout", type=int, default=180)
    args = p.parse_args()
    if min(args.repeats, args.jobs, args.timeout) < 1:
        p.error("repeats, jobs and timeout must be positive")
    try:
        require_process_group_support()
        project_interpreter(ROOT)
    except (OSError, NotImplementedError) as error:
        p.error(str(error))
    cases = [json.loads(line) for line in args.dataset.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = [case for case in cases if not args.case or args.case == case["id"]]
    if not cases:
        p.error("no matching scenarios")
    args.out = args.out.resolve()
    args.out.mkdir(parents=True, exist_ok=True)
    if (args.out / "report.json").exists():
        p.error("output already contains a report; use a new directory")
    version = subprocess.run([args.client, "--version"], capture_output=True, text=True, encoding="utf-8", timeout=10).stdout.strip()
    protocol = args.protocol.read_text(encoding="utf-8")
    report = {"client_version": version, "protocol_version": args.protocol_version,
              "protocol_sha256": hashlib.sha256(protocol.encode()).hexdigest(),
              "dataset_sha256": hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
              "source_root": str(args.source_root.resolve()), "source_sha256": hashlib.sha256("".join(tree_revision(args.source_root / name) for name in ("src", "agents", "skills", "implants", "rules")).encode()).hexdigest(),
              "seed_data": str(args.seed_data.resolve()), "seed_sha256": tree_revision(args.seed_data),
              "instruction_isolation": "macos-global-agents-read-deny" if args.client == "codex" and args.codex_isolate_global_instructions else "claude-bare" if args.client == "claude" else "global-agents-not-isolated", "results": []}
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {executor.submit(run_case, args.client, case, args.out / f"{case['id']}-{repeat}", protocol,
                                   args.timeout, args.source_root.resolve(), args.protocol_version, args.seed_data.resolve(), args.codex_isolate_global_instructions): (case["id"], repeat)
                   for repeat in range(args.repeats) for case in cases}
        for future in as_completed(pending):
            case_id, repeat = pending[future]
            result = future.result()
            result["repeat"] = repeat
            report["results"].append(result)
            report["summary"] = summarize(report["results"])
            (args.out / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"case": case_id, "repeat": repeat, "passed": result["passed"]}), flush=True)
    return 0 if all(r["passed"] for r in report["results"]) else 1


if __name__ == "__main__":
    sys.exit(main())
