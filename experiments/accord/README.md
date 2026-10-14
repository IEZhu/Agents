# Accord Python experiment

A small executable model of [Accord IR 0.1](../../docs/accord-ir.md), built before investing in a compiler. It tests whether explicit task acceptance, immutable candidate identity, conserved budgets and durable action authority make important failures observable. It is isolated from the Agents-Core daemon and package distribution.

## Run

From this repository's worktree root, using Python 3.11 or newer and only its standard library:

```sh
python3 -m experiments.accord
python3 -m unittest discover -s experiments/accord/tests -v
```

The demo creates temporary host and destination databases, generates a document, rejects acceptance without checks, accepts the checked bytes, loses the publication response, reopens both databases and reconciles the result. It finishes with destination version `1`, a spent grant and two remaining request credits. Temporary state is removed at exit; no network, model credentials or external service is used.

To retain state for inspection, construct `Runtime` and `SQLiteSink` with paths in an existing local directory. Store the task/acceptance/grant/operation handles returned by each call. Reopening the same files preserves identity and recorded results; starting new files creates a new simulation. The ledger format is experimental and has no migration compatibility promise.

## Minimal host program

```python
from pathlib import Path
from tempfile import TemporaryDirectory

from experiments.accord.ir import Contract
from experiments.accord.runtime import Runtime, Validator
from experiments.accord.sink import SQLiteSink

contract = Contract.parse(Path("experiments/accord/examples/document.json").read_text())
checks = {
    "nonempty": Validator("1", lambda text, inputs: bool(text.strip())),
    "mentions_spec": Validator("1", lambda text, inputs: inputs["spec"] in text),
}
with TemporaryDirectory() as directory:
    root = Path(directory)
    sink = SQLiteSink(root / "destination.sqlite")
    host = Runtime(root / "host.sqlite", checks, {"docs": sink})
    host.set_resource("spec", "Describe the contract")
    task = host.create_task(contract)
    host.infer(task, "draft", lambda inputs: inputs["spec"] + ": frozen bytes, explicit checks.")
    accepted = host.accept(task, host.check(task))
    # This is a privileged host decision, not a tool offered to the model.
    grant = host.authorize("publish_report", task, accepted, "docs", "report", 0)
    operation = host.prepare(task, accepted, grant)
    result = host.dispatch(operation)
    assert result["status"] == "confirmed"
    assert sink.target("report")["version"] == 1
```

A child task uses `host.create_task(child_contract, parent=task)`. Its request credits are transferred from the parent in one transaction. Child scope must fit the parent's declared inputs, targets and output limit. Returning child text to a parent does not transfer acceptance: the parent must run its own required checks.

`infer` with the same completed attempt label replays the saved result. A new label explicitly resamples and spends another credit. A started/failed attempt cannot be automatically rerun. `recover` reconciles an uncertain action and resends only when the adapter supports durable deduplication and current admission guards pass. A new model attempt and a retried action are different operations.

## What the tests establish

Recorded local run on 2026-10-14: **29 unittest methods passed on Python 3.13.13**. Several methods include multiple adversarial cases. These are deterministic correctness checks, not production reliability measurements or a formal proof.

| Boundary | Executed cases |
|---|---|
| Closed IR | Unknown/missing fields and versions, duplicate keys/resources, wrong numeric types and limits, invalid identifiers, canonical ordering |
| Receipt identity | Missing, forged, failed, duplicate and wrong-check receipts; substitution across tasks, attempts and output bytes |
| Freshness | Changed inputs, input A → B → A, validator/policy/environment profile changes and reversions; dependency change during a validator; resampling after preparation |
| Budget and delegation | Two concurrent children competing for one credit, competing model calls, scope expansion, parent cancellation, permanent charges for invalid outputs |
| Attempt ordering | Concurrent same-attempt calls; late old output cannot replace the newer candidate; persisted output replay avoids a model call |
| Authority | Two intents racing for one logical request, changed intent rejected, repeated/concurrent preparation and dispatch, revocation, target scope, substituted sink identity |
| External effect | Target-version CAS denial, deduplication before CAS, cancellation after admission, opaque sink never blindly retried |
| Recovery | Actual subprocess `os._exit` before send, after sink commit and after model response; reopening state; reconciliation after cancellation/revocation; recovery racing a live original sender |

Each race uses separate SQLite connections, barriers/events and bounded waits. Crash tests terminate a child process with an explicit exit code rather than relying only on a caught exception. The destination database is independent of the host database, so the commit/confirmation gap is real in the experiment. The opaque adapter intentionally hides a successfully applied result and demonstrates the remaining `outcome_unknown` state.

## Layout and trust boundary

- `ir.py`: immutable contract, strict parser and identity encoding.
- `runtime.py`: snapshots, profile epochs, checks, acceptance, budgets, grants and operations.
- `store.py`: serialized SQLite transactions and inspectable event history.
- `sink.py`: separate simulated destination with atomic target CAS and operation deduplication.
- `__main__.py`: runnable lost-response/recovery example.
- `tests/test_runtime.py`: executable counterexamples and subprocess crash tests.

All Python callbacks, the validator registry, host administration APIs and both databases are trusted. There is no containment of arbitrary Python, filesystem or network access, no authenticated cross-host receipts, no provider billing enforcement, no general workflow scheduler and no compiler. Validators establish only their declared predicates; the example's text checks do not establish factual correctness. The example does not publish to a real repository or remote service.

A host policy/input/cancellation change before dispatch admission blocks a new send. A change after admission cannot retract an already admitted write. The destination alone enforces its version precondition at write time. Missing reconciliation data does not establish that an earlier send failed; automatic resend additionally requires durable destination deduplication. No arbitrary external exactly-once guarantee is claimed.

The design hypothesis remains open: this prototype demonstrates a runtime/library boundary, not that a separate language improves developer productivity. Next evaluate the same contracts through two independently implemented adapters and a real bounded repository workflow; compare with an equally protected Python SDK before adding syntax.
