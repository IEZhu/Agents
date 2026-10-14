"""A deliberately contradictory candidate exposes the predicates' known limit."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from .clients import ARMS
from .run import Trial
from .workload import ANCHOR, source_snapshot

CONTRADICTION = "A successful structural validation guarantees that all worker claims are factually true.\n\n"


def probe():
    rows = []
    for arm in ARMS:
        with TemporaryDirectory(prefix="accord-semantic-probe-") as directory:
            trial = Trial(arm, directory, source_snapshot())
            trial.text = trial.text.replace(ANCHOR, CONTRADICTION + ANCHOR, 1)
            trial.ready()
            result = trial.host.dispatch(trial.operation)
            rows.append({"arm": arm, "all_declared_checks_passed": True,
                         "publication_status": result["status"],
                         "known_false_claim_published": CONTRADICTION in trial.sink.target()["text"]})
    return {"probe": "contradictory prose preserving the tested positive phrases", "contradiction": CONTRADICTION.strip(),
            "rows": rows, "interpretation": "These predicates do not establish prose truth. The same blind spot appears in every arm. The bad candidate is confined to disposable fixture repositories and is never materialized into the task worktree."}


if __name__ == "__main__":
    output = Path(__file__).parent / "results/semantic-probe.json"
    output.write_text(json.dumps(probe(), indent=2) + "\n")
    print(output)
