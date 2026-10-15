#!/usr/bin/env python3
"""Opt-in native owner probe. This command samples; it is never an MCP tool."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import secrets
import sys

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.dev.local_inference_diagnostics import PrivateArtifacts, absolute, probe, write_report
from src.local_inference.config import load_owner_config


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--config", required=True, type=absolute, help="Absolute explicit owner JSON config; selects unused port and existing model.")
    result.add_argument("--state-dir", required=True, type=absolute, help="Private owner state; capability records are signed here.")
    result.add_argument("--output", required=True, type=absolute, help="New report file in a private durable directory; never overwritten.")
    result.add_argument("--max-output-tokens", type=int, default=32, choices=range(1, 129), metavar="1..128")
    result.add_argument("--timeout-seconds", type=int, default=120, choices=range(1, 601), metavar="1..600")
    return result


async def run(args):
    if args.output.exists() or args.output.is_symlink():
        raise ValueError("Output already exists.")
    config = load_owner_config(str(args.config))
    if args.max_output_tokens > config.max_output_tokens or args.timeout_seconds > config.max_job_seconds:
        raise ValueError("Diagnostic limits exceed owner ceilings.")
    artifacts = PrivateArtifacts(args.output.parent / f"probe-raw-{secrets.token_hex(8)}")
    report = await probe(config, args.state_dir, artifacts, cap=args.max_output_tokens, timeout=args.timeout_seconds)
    write_report(args.output, report)
    # Raw prompts, model output and owner paths stay in the private artifact.
    print(f"model_calls={report['model_calls']} resident={report['resident']} snapshot={report['snapshot']}")
    return 1 if report["errors"] or not report["owned_backend_closed"] else 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except (OSError, ValueError):
        print("Owner diagnostic failed; verify explicit private paths and configuration.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
