"""Command line of the scheduled runs; every command prints one JSON object.

    python -m src.schedules tick
    python -m src.schedules run autodev [--mode dry-run|run|resume] [--pr N] [--run-id ID]
    python -m src.schedules autodev install [--python PATH] | uninstall
    python -m src.schedules autodev enable --mode dry-run|run [--accept-shared-account] | disable
    python -m src.schedules autodev status | ready
    python -m src.schedules autodev run-now [--mode dry-run|run|resume --pr N]

The exit code is 0 on success, 1 when a command refused or a run failed, and 2 for wrong
arguments. The installation runs it by absolute path as ``<python> -P -m src.schedules …`` with
``PYTHONPATH`` naming the installation. Real modes refuse until #347 and #350 land.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from src.daemon.state import state_dir

from . import runner
from .config import ConfigError

INSTALLATION = Path(__file__).resolve().parents[2]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m src.schedules", description="Scheduled runs (autodev).")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("tick", help="decide whether a run is due and start it")

    run = commands.add_parser("run", help="one run (started by tick and run-now under supervision)")
    run.add_argument("job", choices=["autodev"])
    run.add_argument("--mode", choices=["dry-run", "run", "resume"], default="dry-run")
    run.add_argument("--pr", type=int)
    run.add_argument("--run-id")

    autodev = commands.add_parser("autodev", help="the autodev schedule")
    actions = autodev.add_subparsers(dest="action", required=True)
    install = actions.add_parser("install", help="install the scheduler LaunchAgent")
    install.add_argument("--python", help="the interpreter the scheduler starts (default: this one)")
    actions.add_parser("uninstall", help="remove the scheduler LaunchAgent")
    enable = actions.add_parser("enable", help="turn the schedule on")
    enable.add_argument("--mode", choices=["dry-run", "run"], required=True)
    enable.add_argument("--accept-shared-account", action="store_true")
    actions.add_parser("disable", help="turn the schedule off")
    actions.add_parser("status", help="state, recent runs and the next slot")
    run_now = actions.add_parser("run-now", help="one supervised run now")
    run_now.add_argument("--mode", choices=["dry-run", "run", "resume"], default="dry-run")
    run_now.add_argument("--pr", type=int)
    actions.add_parser("ready", help="check the automatic-mode conditions (a stub until #350)")
    return parser


def dispatch(arguments: argparse.Namespace, ctx: runner.Context) -> tuple[int, dict]:
    if arguments.command == "tick":
        return runner.tick(ctx)
    if arguments.command == "run":
        return runner.run_autodev(ctx, arguments.mode, pr=arguments.pr, run_id=arguments.run_id)
    action = arguments.action
    if action == "install":
        if arguments.python:
            ctx.python = arguments.python
        return 0, {"ok": True, **ctx.scheduler().install(ctx.tool_search)}
    if action == "uninstall":
        return 0, {"ok": True, **ctx.scheduler().uninstall()}
    if action == "enable":
        return runner.enable(ctx, arguments.mode)
    if action == "disable":
        return runner.disable(ctx)
    if action == "status":
        return runner.status(ctx)
    if action == "ready":
        return runner.ready(ctx)
    return runner.run_now(ctx, arguments.mode, arguments.pr)


def main(argv: Sequence[str] | None = None, ctx: runner.Context | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    if getattr(arguments, "mode", None) == "resume" and arguments.pr is None:
        parser.error("--mode resume needs --pr N")
    if getattr(arguments, "run_id", None) and not runner.records.valid_run_id(arguments.run_id):
        parser.error("--run-id is not a run ID")
    ctx = ctx or runner.Context(installation=INSTALLATION, directory=state_dir(INSTALLATION))
    try:
        code, result = dispatch(arguments, ctx)
    except ConfigError as error:
        code, result = 1, {"ok": False, "error": "invalid_config", "problems": error.problems}
    except RuntimeError as error:
        code, result = 1, {"ok": False, "error": type(error).__name__, "message": str(error)}
    json.dump(result, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")
    return code
