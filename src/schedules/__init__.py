"""Scheduled runs of the installation: the autodev runner skeleton (#338, design: docs/autodev-plan.md).

``python -m src.schedules tick`` is started by the LaunchAgent ``local.agents-core.<id>.scheduler``
every 300 seconds and decides cheaply whether a run is due (`tick`). A run (``run autodev``) goes
under supervision (`supervise`) while it holds the run lease (`lease`), checks the runner's code
for drift (`drift`), reads GitHub through a read-only client (`github`) and asks the gate (`gate`)
for a decision. Everything it learns goes into a run record (`records`). Configuration lives in
``<state_dir>/schedules/autodev.json`` (`config`); the reference is docs/autodev-runner.md.

The run sequence (`sequence`, #347) runs the stages: the gate, the select stage, and in a real run
the implement and submit stages with the broker (`broker`) and the worker service (`workers`).
Sandboxed stage sessions reach the runner only through the runner socket (`socket_service`), which
serves the heavy-process lease (`heavy`), the broker and the workers to the thin clients it writes
for each run.

The schedule runs ``dry-run``, or ``run`` in the automatic mode (`automatic`, #350): a scheduled
real run starts only while ``autodev ready`` passes and the owner's acceptance of the residual
risks is current. ``run-now`` starts supervised real runs.
"""
