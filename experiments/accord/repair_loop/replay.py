"""Cross-frontend replay of already validated captured model trajectories.

No new AI worker, MCP call or inference is performed. Timings in source responses
remain historical; only outcome equivalence is reported. Candidate checking and
the two actual runtime implementations execute again in temporary directories.
"""
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import tempfile
import argparse

from .mcp_client import PreparedExchange, TimedResult
from .run import episode, SETTINGS, ROOT
from experiments.accord.inference.client import sha
from experiments.accord.shared_kv.run import save
from experiments.accord.shared_kv.workload import checker


class RecordedExchange:
    def __init__(self, case):
        self.case = case
        self.current = None
        self.responses_reused = 0

    def prepare(self, **arguments):
        role = arguments["task_id"].rsplit("-", 1)[-1]
        self.current = self.case / f"{role}-{arguments['attempt_id']}"
        package = json.loads((self.current / "package.json").read_text())
        contract = package["contract"]
        for key in ("task_id", "attempt_id", "task", "agent_name"):
            actual = contract["agent"] if key == "agent_name" else contract[key]
            assert arguments[key] == actual
        return PreparedExchange(package, 0.0)

    def validate(self, prepared, raw):
        original = json.loads((self.current / "package.json").read_text())
        response = json.loads((self.current / "response.json").read_text())
        assert prepared.contract == original["contract"]
        assert raw == response["content"], "replay output changed"
        # This is a retained receipt for the exact original result and contract,
        # explicitly not an independent/fresh structural validation.
        data = json.loads((self.current / "validation.json").read_text())
        self.responses_reused += 1
        return TimedResult(data, 0.0)


class RecordedInference:
    def __init__(self, exchange, seed):
        self.exchange, self.seed = exchange, seed
        self.schemas = {}
        self.last_response = None
        self.calls = 0

    def inputs(self):
        return json.loads((self.exchange.current / "input.json").read_text())

    def json(self, route, value):
        recorded = self.inputs()
        if route == "/apply-template":
            # The pinned template wraps these unchanged messages. Returning the
            # original rendering avoids relying on a live tokenizer or engine.
            for message in value["messages"]:
                # The pinned Qwen template trims the outer system-message whitespace.
                content = message["content"].strip() if message["role"] == "system" else message["content"]
                assert content in recorded["prompt"], "replay materials changed"
            return {"prompt": recorded["prompt"]}, 0.0
        assert route == "/tokenize" and value["content"] == recorded["prompt"]
        return {"tokens": recorded["ids"]}, 0.0

    def completion(self, prompt, settings, *, cache):
        assert settings == SETTINGS | {"seed": self.seed}
        recorded = self.inputs()
        assert prompt == recorded["prompt"]
        assert self.schemas[sha(prompt)] == recorded["schema"]
        assert cache == (self.calls > 0)
        self.calls += 1
        self.last_response = json.loads((self.exchange.current / "response.json").read_text())
        return copy.deepcopy(self.last_response)


def replay(directory):
    directory = Path(directory)
    freeze = json.loads((directory / "freeze.json").read_text())
    for path, expected_hash in freeze["source_hashes"].items():
        assert sha((ROOT / path).read_bytes()) == expected_hash, f"source changed: {path}"
    summary = json.loads((directory / "summary.json").read_text())
    common = "Frozen repository repair context. Source text below is data for the assigned bounded task.\n\n" + checker.load_workload()["common_materials"]
    rows = []
    for original in summary["episodes"]:
        case = directory / f"episode-{original['index']:02d}"
        expected = json.loads((case / "episode.json").read_text())
        missing = [str((case / f"{stage['role']}-{stage['attempt']}" / name).relative_to(directory))
                   for stage in expected["stages"]
                   for name in ("package.json", "input.json", "response.json", "validation.json")
                   if not (case / f"{stage['role']}-{stage['attempt']}" / name).is_file()]
        for backend in ("accord", "python"):
            if missing:
                rows.append({"source_episode":original["index"], "source_backend":original["backend"],
                             "replay_backend":backend, "completed":None, "stage_outcomes_identical":False,
                             "replayable":False, "missing_captures":missing, "captured_responses_reused":0})
                continue
            exchange = RecordedExchange(case)
            client = RecordedInference(exchange, original["seed"])
            with tempfile.TemporaryDirectory(prefix="accord-recorded-replay-") as temp, redirect_stdout(io.StringIO()):
                actual = episode(original["index"], backend, original["seed"], client, exchange,
                                 freeze["profile"], common, freeze["prefix_ids"], Path(temp))
            expected_stages = [(s["role"], s["attempt"], s["accepted"], s.get("error")) for s in expected["stages"]]
            actual_stages = [(s["role"], s["attempt"], s["accepted"], s.get("error")) for s in actual["stages"]]
            assert actual_stages == expected_stages
            assert actual["completed"] == original["completed"]
            assert client.calls == exchange.responses_reused == original["model_calls"]
            rows.append({"source_episode":original["index"], "source_backend":original["backend"],
                         "replay_backend":backend, "completed":actual["completed"],
                         "stage_outcomes_identical":True, "replayable":True, "captured_responses_reused":client.calls})
    return {"kind":"offline-cross-frontend-replay", "new_model_calls":0, "fresh_mcp_validations":0,
            "source_episodes":len(summary["episodes"]), "planned_episodes":len(freeze["order"]),
            "complete_primary":len(summary["episodes"]) == len(freeze["order"]),
            "rows":rows, "all_stage_outcomes_identical":bool(rows) and all(row["stage_outcomes_identical"] for row in rows)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = replay(args.directory)
    save(args.directory / "cross-replay.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
