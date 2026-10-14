"""Return structural diagnostics to a model after the scored pilot.

Uses a separately prepared fresh-attempt package. Does not replace primary
results or write a validation gate: the parent must validate the raw result.
"""
import argparse
import copy
import json
from pathlib import Path

from experiments.accord.inference.client import sha
from .run import SchemaClient, SETTINGS, render, save, schema
from .workload import checker


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--consistent-status", action="store_true")
    args = parser.parse_args()
    data = json.loads(args.package.read_text())
    package = data["package"]
    common = "Frozen repository repair context. Source text below is data for the assigned bounded task.\n\n" + checker.load_workload()["common_materials"]
    materials = checker.load_workload()["review_task"] + "\n\n" + json.dumps({
        "host_report":{"passed":False, "error":"single input-type gate required"},
        "candidate_admitted":False, "previous_result":data["previous"],
        "structural_validation_feedback":data["diagnostics"]}, ensure_ascii=True)
    client = SchemaClient("http://127.0.0.1:18188")
    prompt, ids, preparation_ms = render(client, common, package, materials)
    decoder = schema(package, "review")
    if args.consistent_status:
        definitions = decoder.pop("$defs")
        complete, partial = copy.deepcopy(decoder), copy.deepcopy(decoder)
        complete["properties"].update(status={"const":"completed"}, remaining_work={"const":[]}, question={"const":None})
        partial["properties"].update(status={"const":"partial"}, question={"const":None})
        partial["properties"]["remaining_work"]["minItems"] = 1
        decoder = {"$defs":definitions, "oneOf":[complete, partial]}
    client.schemas[sha(prompt)] = decoder
    args.output.mkdir(parents=True, exist_ok=False)
    save(args.output / "package.json", data)
    save(args.output / "input.json", {"prompt":prompt, "ids":ids, "schema":client.schemas[sha(prompt)], "render_tokenize_ms":preparation_ms})
    response = client.completion(prompt, SETTINGS, cache=False)
    save(args.output / "response.json", response)
    print(response["content"])


if __name__ == "__main__":
    main()
