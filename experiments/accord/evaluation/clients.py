"""Three explicitly configured clients; shared task checks and publication adapter."""

from pathlib import Path

from ..ir import Contract
from ..runtime import Runtime
from .workload import checks

ARMS = ("accord_json", "accord_python", "python_sdk")


def python_configuration():
    return dict(name="subagent_testing_guide",
                inputs=("test_guide", "core_tests", "mcp_tests", "api_guide", "project_config", "test_evidence"),
                output_max_bytes=65536,
                checks=(("bounded_edit", "1"), ("runnable_command", "2"),
                        ("source_alignment", "1"), ("focused_tests", "1")),
                publish_to=("test_guide",), request_budget=2, policy_revision="p1")


def json_contract():
    return Contract.parse((Path(__file__).parent / "contract.json").read_text())


def python_contract():
    return Contract(**python_configuration())


def sdk_specification():
    from .python_sdk import JobSpec
    return JobSpec(**python_configuration())


def open_client(arm, state_path, sink, validators=None):
    registry = checks() if validators is None else validators
    if arm == "python_sdk":
        from .python_sdk import PythonSDK
        return PythonSDK(state_path, registry, {"docs": sink}), sdk_specification()
    if arm == "accord_json":
        return Runtime(state_path, registry, {"docs": sink}), json_contract()
    if arm == "accord_python":
        return Runtime(state_path, registry, {"docs": sink}), python_contract()
    raise ValueError(f"unknown evaluation arm: {arm}")
