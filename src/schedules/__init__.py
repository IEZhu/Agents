"""Scheduled runs of the installation: the autodev runner skeleton (#338, design: docs/autodev-plan.md).

``python -m src.schedules tick`` is started by the LaunchAgent ``local.agents-core.<id>.scheduler``
every 300 seconds and decides cheaply whether a run is due (`tick`). A run (``run autodev``) goes
under supervision (`supervise`) while it holds the run lease (`lease`), checks the runner's code
for drift (`drift`), reads GitHub through a read-only client (`github`) and asks the gate (`gate`)
for a decision. Everything it learns goes into a run record (`records`). Configuration lives in
``<state_dir>/schedules/autodev.json`` (`config`); the reference is docs/autodev-runner.md.

Only ``dry-run`` works in this part: every real mode refuses and names the parts it needs.
"""
