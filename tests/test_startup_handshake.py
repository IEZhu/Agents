"""The stdio handshake must not wait for the embedding model (readiness gate).

Spawns ``src/server.py`` from a temporary copy of the installation with a fake
``fastembed`` whose ``TextEmbedding()`` takes 20 s to load (sleeping, or holding
the GIL) and asserts that ``initialize`` and ``tools/list`` still answer at once
and that a gated tool returns ``warming_up`` within its cap.
"""
import json
import shutil
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.slow, pytest.mark.asyncio]

ROOT = Path(__file__).resolve().parents[1]
FAKE_FASTEMBED = '''
import os, time
import numpy as np


class TextEmbedding:
    def __init__(self, *args, **kwargs):
        end = time.monotonic() + float(os.environ["FAKE_LOAD_SECONDS"])
        if os.environ.get("FAKE_HOLD_GIL") == "1":
            while time.monotonic() < end:
                pass
        else:
            time.sleep(max(0.0, end - time.monotonic()))

    @staticmethod
    def _vector(text):
        generator = np.random.default_rng(sum(map(ord, text)))
        vector = generator.random(8)
        return vector / np.linalg.norm(vector)

    def passage_embed(self, texts, **kwargs):
        return (self._vector(text) for text in texts)

    def query_embed(self, texts):
        return (self._vector(text) for text in texts)
'''
HANDSHAKE_LIMIT_SECONDS = 8.0


@pytest.fixture(scope="module")
def installation(tmp_path_factory):
    root = tmp_path_factory.mktemp("install")
    for folder in ("src", "agents", "skills", "implants", "rules", "flows"):
        shutil.copytree(ROOT / folder, root / folder, ignore=shutil.ignore_patterns("__pycache__"))
    fake = root / "fake"
    (fake / "fastembed").mkdir(parents=True)
    (fake / "fastembed" / "__init__.py").write_text(FAKE_FASTEMBED)
    (fake / "fastembed-0.0.0.dist-info").mkdir()
    (fake / "fastembed-0.0.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: fastembed\nVersion: 0.0.0\n")
    return root


async def _session(installation, *, load_seconds, hold_gil=False, cap=2):
    import os
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        **os.environ,
        "PYTHONPATH": str(installation / "fake"),
        "FAKE_LOAD_SECONDS": str(load_seconds),
        "FAKE_HOLD_GIL": "1" if hold_gil else "0",
        "WARMUP_WAIT_SECONDS": str(cap),
        "AGENTS_CLIENT_REPO_ROOT": str(installation),
        "AGENTS_AUTO_UPDATE": "0",
        "EMBEDDING_MODEL": "fake/model",
        "LANGFUSE_TRACING_ENABLED": "false",
    }
    params = StdioServerParameters(
        command=sys.executable, args=[str(installation / "src" / "server.py")], env=env)
    return stdio_client(params), ClientSession


@pytest.mark.parametrize("hold_gil", [False, True], ids=["sleeping-load", "gil-holding-load"])
async def test_handshake_and_gated_call_with_a_slow_model(installation, hold_gil):
    transport, ClientSession = await _session(installation, load_seconds=20, hold_gil=hold_gil)
    started = time.monotonic()
    async with transport as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert time.monotonic() - started < HANDSHAKE_LIMIT_SECONDS
            assert "route_and_load" in {tool.name for tool in tools.tools}

            called = time.monotonic()
            result = await session.call_tool("route_and_load", {"query": "hello", "protocol_version": 2})
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "ERROR" and payload["message"].startswith("warming_up")
            assert time.monotonic() - called < 2 + HANDSHAKE_LIMIT_SECONDS

            agents = await session.call_tool("list_agents", {"include_metadata": False})
            assert json.loads(agents.content[0].text)["agents"]


async def test_gated_call_succeeds_once_the_model_is_ready(installation):
    transport, ClientSession = await _session(installation, load_seconds=1, cap=60)
    async with transport as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("route_and_load", {"query": "hello", "protocol_version": 2})
            payload = json.loads(result.content[0].text)
            assert payload["status"] in {"SUCCESS", "ROUTE_REQUIRED"}, payload
