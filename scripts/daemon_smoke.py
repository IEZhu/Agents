#!/usr/bin/env python3
"""Opt-in real-model/real-socket integration; all project writes stay in temp dirs."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import signal
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
from src.daemon.state import write_json, atomic_private
from src.daemon.workspaces import WorkspaceRegistry


async def smoke(port=18765, soak=False):
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="agents-daemon-smoke-") as temporary:
        base = Path(temporary)
        state = base / "state"; state.mkdir(mode=0o700)
        cache = Path.home() / ".cache/fastembed/models--qdrant--multilingual-e5-large-onnx"
        revision = (cache / "refs/main").read_text().strip()
        config = {"installation": str(root), "port": port, "model": "intfloat/multilingual-e5-large",
                  "model_cache": str(cache.parent), "path": os.environ["PATH"],
                  "model_path": str(cache / "snapshots" / revision),
                  "model_artifact": cache.name + ":" + revision}
        write_json(state / "service.json", config)
        token = secrets.token_urlsafe(48)
        atomic_private(state / "token", token)
        projects = [base / "a", base / "b"]
        for project in projects:
            project.mkdir(); (project / "README.md").write_text("Identical source\n")
            (project / "CLAUDE.md").write_text("Keep this user-owned content.\n")
        ids = [WorkspaceRegistry(state).register(project) for project in projects]
        start = time.monotonic()
        process = subprocess.Popen([sys.executable, "-m", "src.daemon", "--state", str(state), "serve"], cwd=root)
        headers = {"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"}
        url = f"http://127.0.0.1:{port}"
        try:
            async with httpx.AsyncClient(base_url=url, headers=headers, timeout=120) as http:
                health = {}
                while time.monotonic() - start < 120:
                    if process.poll() is not None: raise RuntimeError((state / "service.log").read_text()[-4000:])
                    try:
                        response = await http.get("/health")
                        health = response.json()
                        if health["state"] == "ready": break
                        if health["state"] == "failed": raise RuntimeError((state / "service.log").read_text()[-4000:])
                    except httpx.ConnectError: pass
                    await asyncio.sleep(.1)
                assert health.get("state") == "ready", "warmup timeout"
                startup = time.monotonic() - start
                request_id = 0
                async def call(name, arguments, workspace=None, client=http):
                    nonlocal request_id
                    request_id += 1
                    response = await client.post("/mcp", headers={"X-Agents-Workspace": workspace} if workspace else {},
                        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}})
                    response.raise_for_status()
                    assert "mcp-session-id" not in response.headers
                    result = response.json()["result"]
                    assert not result.get("isError"), result
                    return json.loads(result["content"][0]["text"])
                tools = (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 0, "method": "tools/list"})).json()["result"]["tools"]
                assert "clear_session_cache" not in {tool["name"] for tool in tools}
                missing = await call("read_history", {})
                assert missing.get("error") == "workspace_required"
                invalid = await call("read_history", {}, "invalid")
                assert invalid.get("error") == "workspace_invalid"
                await asyncio.gather(*(call("log_interaction", {"agent_name": "software_engineer", "query": label,
                    "response_content": "answer " + label}, identity) for label, identity in zip(["A", "B"], ids)))
                history = await asyncio.gather(*(call("read_history", {}, identity) for identity in ids))
                assert history[0]["entries"][0]["intent"] == "A"
                assert history[1]["entries"][0]["intent"] == "B"
                descriptions = await asyncio.gather(*(call("describe_repo", {}, identity) for identity in ids))
                assert all(value["status"] == "needs_summary" for value in descriptions)
                original = descriptions[0]
                summary = "## Project\n" + "overview " * 210
                arguments = {key: original[key] for key in ("workspace_id", "repo_path", "repo_hash")}
                arguments["summary"] = summary
                wrong = await call("write_repo_summary", arguments, ids[1])
                assert wrong["status"] == "error"
                (projects[0] / "README.md").write_text("Changed source\n")
                stale = await call("write_repo_summary", arguments, ids[0])
                assert stale["status"] == "rejected"
                fresh = await call("describe_repo", {}, ids[0])
                arguments.update({key: fresh[key] for key in ("workspace_id", "repo_path", "repo_hash")})
                good = await call("write_repo_summary", arguments, ids[0])
                assert good["status"] == "refreshed"
                assert (projects[0] / "CLAUDE.md").read_text().startswith("Keep this user-owned content.")
                query = "Implement a Python API request handler and tests"
                await call("get_agent_context", {"agent_name": "software_engineer", "query": query})
                async def connected(index):
                    async with httpx.AsyncClient(base_url=url, headers=headers, timeout=120) as client:
                        began = time.monotonic()
                        response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": {
                            "protocolVersion": "2025-11-25", "capabilities": {"sampling": {}, "roots": {}},
                            "clientInfo": {"name": "smoke", "version": "1"}}})
                        response.raise_for_status()
                        result = await call("route_and_load", {"query": query}, client=client)
                        assert result["status"] != "SUCCESS_SAMPLED"
                        return time.monotonic() - began
                sequential = [await connected(i) for i in range(20)]
                concurrent = await asyncio.gather(*(connected(i) for i in range(20)))
                p95 = lambda values: sorted(values)[int(len(values) * .95) - 1]
                health_metrics = (await http.get("/health")).json()
                health_metrics.pop("install_root", None)
                result = {"startup_seconds": startup, "pid": process.pid, "clients": 20,
                          "sequential_init_route_p95_seconds": p95(sequential),
                          "concurrent_init_route_p95_seconds": p95(concurrent),
                          "health": health_metrics}
                if soak:
                    from daemon_baseline import snapshot
                    before = snapshot(root)
                    for i in range(2, 20):
                        project = base / f"workspace-{i}"; project.mkdir()
                        identity = WorkspaceRegistry(state).register(project)
                        ids.append(identity)
                        await call("log_interaction", {"agent_name": "software_engineer", "query": f"workspace {i}", "response_content": "one entry"}, identity)
                    for identity in ids:
                        await call("read_history", {"query": "workspace history"}, identity)
                    for batch in range(50):
                        await asyncio.gather(*(connected(i) for i in range(20)))
                    after = snapshot(root)
                    result["soak"] = {"connections": 1000, "workspaces": 20,
                                      "before_footprint_bytes": before["total_physical_footprint_bytes"],
                                      "after_footprint_bytes": after["total_physical_footprint_bytes"]}
                    def cpu_seconds():
                        value = subprocess.check_output(["/bin/ps", "-p", str(process.pid), "-o", "time="], text=True).strip()
                        fields = [float(v) for v in value.replace("-", ":").split(":")]
                        return sum(v * 60**i for i, v in enumerate(reversed(fields)))
                    cpu_start = cpu_seconds()
                    idle_start = time.monotonic()
                    await asyncio.sleep(300)
                    result["soak"]["idle_cpu_percent_one_core"] = 100 * (cpu_seconds() - cpu_start) / (time.monotonic() - idle_start)
                    result["soak"]["idle_seconds"] = time.monotonic() - idle_start
                try:
                    from daemon_baseline import snapshot
                    result["footprint"] = snapshot(root)
                except (OSError, subprocess.SubprocessError): pass
                print(json.dumps(result, indent=2))
                await http.post("/admin/drain")
                assert (await http.post("/mcp", json={})).status_code == 503
        finally:
            process.send_signal(signal.SIGTERM)
            try: process.wait(timeout=65)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
                raise AssertionError("Graceful shutdown timed out")


if __name__ == "__main__":
    asyncio.run(smoke(soak="--soak" in sys.argv))
