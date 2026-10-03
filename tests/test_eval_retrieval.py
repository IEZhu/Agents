"""run_retrieval's command line; no model is loaded."""
from __future__ import annotations

import json

import pytest

from evals.runners import run_retrieval


def test_a_dataset_given_twice_is_refused(tmp_path):
    path = tmp_path / "set.jsonl"
    path.write_text(json.dumps({"id": "a", "language": "en", "expected_agent": "lawyer", "query": "q"}) + "\n",
                    encoding="utf-8")
    with pytest.raises(SystemExit, match="only once"):
        run_retrieval.main(["--dataset", str(path), "--dataset", str(tmp_path / "." / "set.jsonl")])
