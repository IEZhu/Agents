"""Run a deterministic example with a lost sink reply and durable recovery."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .ir import AccordError, Contract
from .runtime import Runtime, Validator
from .sink import SQLiteSink


def demo(directory):
    contract = Contract.parse((Path(__file__).parent / "examples/document.json").read_text())
    validators = {"nonempty": Validator("1", lambda text, inputs: bool(text.strip())),
                  "mentions_spec": Validator("1", lambda text, inputs: inputs["spec"] in text)}
    sink = SQLiteSink(directory / "destination.sqlite")
    runtime = Runtime(directory / "host.sqlite", validators, {"docs": sink})
    runtime.set_resource("spec", "Explain revision-bound acceptance")
    task = runtime.create_task(contract)
    candidate = runtime.infer(task, "draft", lambda inputs: inputs["spec"] + ". Checked bytes are published.")
    try:
        runtime.accept(task, [])
    except AccordError as exc:
        print(f"Missing checks rejected: {exc}")
    accepted = runtime.accept(task, runtime.check(task))
    grant = runtime.authorize("publish_report", task, accepted, "docs", "report", 0)
    operation = runtime.prepare(task, accepted, grant)

    def lose_reply(phase):
        if phase == "after_send":
            raise ConnectionError("simulated lost response")

    print("After lost reply:", runtime.dispatch(operation, crash_hook=lose_reply)["status"])
    # Reopen both stores: recovery must work without the old runtime object's memory.
    reopened = Runtime(directory / "host.sqlite", validators, {"docs": SQLiteSink(directory / "destination.sqlite")})
    print("After recovery:", reopened.recover(operation)["status"])
    replay = reopened.infer(task, "draft", lambda _: (_ for _ in ()).throw(AssertionError("model replayed")))
    assert replay == candidate
    print("Recorded output replayed without another model call.")
    print(json.dumps({"destination": sink.target("report"), "requests_remaining": reopened.inspect("task", task)["remaining"],
                      "grant_state": reopened.inspect("grant", grant)["state"], "events": len(reopened.events())}, indent=2))


def main():
    with TemporaryDirectory(prefix="accord-demo-") as directory:
        demo(Path(directory))


if __name__ == "__main__":
    main()
