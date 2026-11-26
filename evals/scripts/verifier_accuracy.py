"""Command line of the verifier-accuracy measurement (docs/verifier-accuracy-measurement.md).

Run from the repository root as ``python -m evals.scripts.verifier_accuracy <command>``;
flows/verifier-accuracy.md says when to run each command. Every command prints aggregates
only, never a statement, a label or a per-item verdict, so a parent model can read its
output without unblinding itself.

  build      export the pinned commit and write a new set into the control directory
  simulate   coverage simulation at the priors and at b = 0.995; sets the floor under D
  probe      one canary probe: a sandboxed worker must fail to read the canary file
  run        every scheduled attempt not yet recorded, a probe before each batch;
             --stage-runs stops at the leading complete clusters that many runs hold
             on average; a seed other than the set's recorded one is refused
  analyze    the report: fits per configuration and family, checks, strata, cost;
             --look interim fits the first half alone, --look planning adds the
             projection of the whole set from a stage
  status     attempt counts and cost so far
  discard    move the current set aside inside the control directory
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from evals.verifier_accuracy import analysis, builder, control, runner, worker
from src.runtime_paths import installation_root

REPOSITORY = Path(__file__).resolve().parents[2]


def _roots(arguments) -> list[Path]:
    return sorted({Path(arguments.installation).resolve(), Path(installation_root()).resolve(), REPOSITORY})


def _control(arguments) -> Path:
    if arguments.control_dir:
        return Path(arguments.control_dir).resolve()
    return control.default_control_dir(arguments.installation)


def _positive(value: str) -> int:
    """A count of at least 1: zero or a negative value would run or simulate nothing and still succeed."""
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, not {number}")
    return number


def _print(value) -> None:
    print(json.dumps(value, indent=2, sort_keys=True, default=str))


def _launcher(arguments, loaded: runner.LoadedSet) -> worker.HeadlessLauncher:
    return worker.HeadlessLauncher(control_dir=loaded.control_dir, snapshot=loaded.snapshot, claude=arguments.claude,
                                   home=Path.home(), max_budget_usd=arguments.max_run_usd)


def build(arguments) -> int:
    snapshots = (Path(arguments.snapshots_dir).resolve() if arguments.snapshots_dir
                 else control.default_snapshots_dir(arguments.installation))
    manifest = builder.build(repo=Path(arguments.repo), commit=arguments.commit, control_dir=_control(arguments),
                             snapshots=snapshots, name=arguments.name, items=arguments.items, seed=arguments.seed,
                             v2_per_class=arguments.v2_per_class, installation_roots=_roots(arguments),
                             claims_store=Path(arguments.claims_store) if arguments.claims_store else None,
                             same_bundle=worker.bundle_guard)
    _print(manifest)
    return 0


def _sizes(statements: list[dict], v2_only: bool) -> list[int]:
    counts: dict[str, int] = {}
    for row in statements:
        if not v2_only or row["v2"]:
            counts[row["cluster"]] = counts.get(row["cluster"], 0) + 1
    return sorted(counts.values())


def simulate(arguments) -> int:
    control_dir = _control(arguments)
    if arguments.plan_items:
        size = arguments.plan_cluster_size
        model_sizes = [size] * (arguments.plan_items // size)
        execution_sizes = [size] * (arguments.plan_v2_items // size)
        destination = None
    else:
        loaded = runner.load(control_dir, installation_roots=_roots(arguments))
        statements = list(loaded.statements.values())
        model_sizes, execution_sizes = _sizes(statements, False), _sizes(statements, True)
        destination = control_dir / control.SIMULATION
    result = analysis.calibrate(analysis.scenarios(model_sizes, execution_sizes), sets=arguments.sets,
                                resamples=arguments.resamples, look=arguments.look, seed=arguments.seed)
    if destination is not None:
        result["set"] = loaded.set_id
        control.write_private(destination, json.dumps(result, indent=2, sort_keys=True) + "\n")
    _print(result)
    return 0 if result["held"] else 1


def probe(arguments) -> int:
    loaded = runner.load(_control(arguments), installation_roots=_roots(arguments))
    launcher = _launcher(arguments, loaded)
    model = arguments.model or runner.Runner(loaded, launcher)._probe_model()
    result = runner.probe(loaded, launcher, model=model, effort=None, timeout=arguments.timeout)
    runner.record_probe(loaded, result, model=model, batch=None)  # its cost counts towards the set's spend
    _print({"model": model, **result.__dict__})
    return 0 if result.blind else 2


def _simulation(control_dir: Path, loaded: runner.LoadedSet) -> dict | None:
    """This set's final-look simulation, or None after saying why there is none to use."""
    path = control_dir / control.SIMULATION
    simulation = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if simulation.get("set") != loaded.set_id:
        print("Run `simulate` for this set first: its floor under D comes from that run.", file=sys.stderr)
        return None
    if simulation.get("look") != "final":
        print("This needs the final look's simulation; this one is for the interim quantiles.", file=sys.stderr)
        return None
    return simulation


def run(arguments) -> int:
    control_dir = _control(arguments)
    loaded = runner.load(control_dir, installation_roots=_roots(arguments))
    simulation = _simulation(control_dir, loaded)
    if simulation is None:
        return 1
    if not simulation.get("held") and not arguments.pipeline_check:
        print("The coverage simulation did not hold for this set; its bands would not hold their level.",
              file=sys.stderr)
        return 1
    worker_runner = runner.Runner(loaded, _launcher(arguments, loaded), concurrency=arguments.concurrency,
                                  batch_size=arguments.batch_size, timeout=arguments.timeout,
                                  max_total_usd=arguments.max_total_usd, seed=arguments.seed,
                                  stage_runs=arguments.stage_runs, log=lambda line: print(line, flush=True))
    try:
        summary = worker_runner.run()
    except runner.ScheduleError as error:  # raised before the first probe: nothing ran or was paid for
        print(error, file=sys.stderr)
        return 1
    _print(summary)
    return 0


def _records(control_dir: Path) -> tuple[list[dict], list[dict]]:
    attempts = control_dir / control.ATTEMPTS
    probes = control_dir / control.PROBES
    return (control.read_jsonl(attempts) if attempts.exists() else [],
            control.read_jsonl(probes) if probes.exists() else [])


def analyze(arguments) -> int:
    control_dir = _control(arguments)
    loaded = runner.load(control_dir, installation_roots=_roots(arguments))
    rows, probes = _records(control_dir)
    simulation = _simulation(control_dir, loaded)  # a missing or another set's floor would mislabel every band
    if simulation is None:
        return 1
    first_half = None
    if arguments.look == "interim":
        own = [row for row in rows if row.get("set") == loaded.set_id]
        try:
            first_half = {slot.key for slot in runner.first_half(list(loaded.statements.values()), own)}
        except runner.ScheduleError as error:
            print(error, file=sys.stderr)
            return 1
    report = analysis.analyze(rows, loaded.statements, look=arguments.look, d_floor=simulation["d_floor"],
                              seed=arguments.seed, resamples=arguments.resamples, probes=probes,
                              first_half=first_half)
    if first_half is not None:
        report["first_half"]["schedule_seed"] = runner.schedule_seed(own)
    report["set"] = loaded.set_id
    report["simulation_held"] = simulation.get("held")
    if arguments.look == "planning":
        report["planning"] = analysis.planning(rows, loaded.statements, report, d_floor=simulation["d_floor"],
                                               sets=arguments.projection_sets, seed=arguments.seed)
    if arguments.output:
        Path(arguments.output).write_text(json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
                                          encoding="utf-8")
    _print(report)
    return 0


def status(arguments) -> int:
    control_dir = _control(arguments)
    loaded = runner.load(control_dir, installation_roots=_roots(arguments))
    rows, probes = _records(control_dir)
    scheduled = len(runner.schedule(list(loaded.statements.values()), 0))
    _print({"set": loaded.set_id, "scheduled": scheduled, **analysis.tally(rows),
            "cost": analysis.cost(rows, probes), "probes": [probe["reason"] for probe in probes]})
    return 0


def discard(arguments) -> int:
    print(builder.discard(_control(arguments)))
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    top.add_argument("--installation", default=str(installation_root()),
                     help="Installation whose service-state directory holds the control directory.")
    top.add_argument("--control-dir", help="Override the control directory (tests only).")
    commands = top.add_subparsers(dest="command", required=True)

    making = commands.add_parser("build")
    making.add_argument("--repo", default=str(REPOSITORY))
    making.add_argument("--commit", default="HEAD")
    making.add_argument("--name", required=True)
    making.add_argument("--items", type=int, required=True)
    making.add_argument("--seed", type=int, required=True)
    making.add_argument("--v2-per-class", type=int, help="Cap on V2 items per class; all command-checkable by default.")
    making.add_argument("--claims-store", help="A claims.jsonl whose subjects' files lead the selection.")
    making.add_argument("--snapshots-dir", help="Override the snapshot directory (tests only).")
    making.set_defaults(handler=build)

    simulating = commands.add_parser("simulate")
    simulating.add_argument("--sets", type=int, default=None,
                            help="Simulated sets per scenario; by default 1000 for the final look and 8080 "
                                 "for the interim one, whose 99.9 %% level needs at least 4040.")
    simulating.add_argument("--resamples", type=int, default=200)
    simulating.add_argument("--look", choices=analysis.SIMULATED_LOOKS, default="final")
    simulating.add_argument("--seed", type=int, default=0)
    simulating.add_argument("--plan-items", type=int, help="Simulate a planned design instead of the current set.")
    simulating.add_argument("--plan-v2-items", type=int, default=200)
    simulating.add_argument("--plan-cluster-size", type=int, default=2)
    simulating.set_defaults(handler=simulate)

    for name, handler in (("probe", probe), ("run", run)):
        command = commands.add_parser(name)
        command.add_argument("--claude", help="The Claude Code executable; found on PATH by default.")
        if name == "probe":  # run always probes on the verifier package's own model
            command.add_argument("--model", help="Probe model; the verifier package's model by default.")
        command.add_argument("--timeout", type=float, default=900.0, help="Seconds before a worker is unfinished.")
        command.add_argument("--max-run-usd", type=float, default=2.0, help="Budget of one worker run.")
        command.set_defaults(handler=handler)
        if name == "run":
            command.add_argument("--concurrency", type=int, default=4)
            command.add_argument("--batch-size", type=int, default=20)
            command.add_argument("--max-total-usd", type=float, default=40.0)
            command.add_argument("--seed", type=int, default=0,
                                 help="The schedule's seed; every record names it, and a set's later runs "
                                      "must use the seed of its first.")
            command.add_argument("--stage-runs", type=_positive,
                                 help="Stop at the leading complete clusters of the schedule, as many as this "
                                      "many runs hold on average (runs x clusters // slots), so every cluster "
                                      "is as likely to be in the stage; its slots vary with the clusters drawn "
                                      "and can exceed the value. A later run with a larger value or none "
                                      "continues.")
            command.add_argument("--pipeline-check", action="store_true",
                                 help="Run although the coverage simulation did not hold: a check of the "
                                      "runner whose bands are not a measurement.")

    analyzing = commands.add_parser("analyze")
    analyzing.add_argument("--look", choices=sorted(analysis.QUANTILES), default="final")
    analyzing.add_argument("--seed", type=int, default=0,
                           help="Resampling seed; the interim look reads the schedule's seed from the records.")
    analyzing.add_argument("--resamples", type=int, default=400)
    analyzing.add_argument("--projection-sets", type=_positive, default=200,
                           help="Simulated whole sets behind the planning look's projection.")
    analyzing.add_argument("--output", help="Also write the report (aggregates only) to this file.")
    analyzing.set_defaults(handler=analyze)

    commands.add_parser("status").set_defaults(handler=status)
    commands.add_parser("discard").set_defaults(handler=discard)
    return top


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
