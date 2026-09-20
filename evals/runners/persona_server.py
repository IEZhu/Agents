"""Run the real MCP server in an isolated workspace and record actual calls.

``--source-root`` may point at a git-archive baseline. The trace wrapper is the
only instrumentation; it does not alter instructions, decisions, or results.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]


def json_value(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"Unsupported MCP result type: {type(value).__name__}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, default=ROOT)
    parser.add_argument("--seed-data", type=Path, default=ROOT / "data")
    args = parser.parse_args()
    def startup(stage):
        with (args.workspace / "startup.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"stage": stage, "pid": os.getpid(), "time": time.time()}) + "\n")

    startup("started")
    sys.path.insert(0, str(args.source_root.resolve()))
    os.environ.update(LANGFUSE_TRACING_ENABLED="false", AGENTS_DEBUG="0",
                      AGENTS_AUTO_UPDATE="0", AGENTS_CLIENT_REPO_ROOT=str(args.workspace))
    from src.engine import config
    data = args.workspace / "data"
    if not data.exists():
        data.mkdir()
        if args.seed_data.exists():
            for source in args.seed_data.iterdir():
                if source.is_file() and (source.suffix in {".npz", ".json"} or source.name.startswith(".")):
                    shutil.copy2(source, data / source.name)
    config.DATA_DIR = config.INSTALL_DATA_DIR = str(data)
    from src import server
    startup("server_imported")
    call = server.mcp._tool_manager.call_tool

    async def traced(name, arguments, context=None, convert_result=False):
        started = time.monotonic()
        record = {"tool": name, "arguments": arguments}
        try:
            result = await call(name, arguments, context, convert_result)
            serialized = json.dumps(result, ensure_ascii=False, default=json_value)
            record.update(result=json.loads(serialized), result_bytes=len(serialized.encode()))
            return result
        except Exception as error:
            record["error"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            record["elapsed_ms"] = round(1000 * (time.monotonic() - started))
            with (args.workspace / "trace.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    server.mcp._tool_manager.call_tool = traced
    server.mcp.run()


if __name__ == "__main__":
    main()
