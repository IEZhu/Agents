"""The run state file and its read-only copy (src/schedules/run_state.py, #347)."""
import json
import stat

import pytest

from src.schedules import run_state, stages
from tests.test_schedules_report import NOW, SHA, TOKEN, state


# --- the run state ----------------------------------------------------------------------------------

def test_write_state_writes_the_record_and_a_read_only_copy(tmp_path):
    run_dir, copy_dir = tmp_path / "run", tmp_path / "copy"
    run_dir.mkdir()
    written = run_state.write_state(run_dir, state(), copy_dir, now=NOW)
    assert written == json.loads((run_dir / "state.json").read_text()) == json.loads((copy_dir / "state.json").read_text())
    assert stat.S_IMODE((copy_dir / "state.json").stat().st_mode) == 0o400
    assert stat.S_IMODE((run_dir / "state.json").stat().st_mode) == 0o600
    assert set(written) == {"version", "run_id", "mode", "repository", "pinned", "runner_commit", "stage", "stages_done",
                            "issue", "branch", "pr", "base", "head", "slice", "criteria_refs", "flows",
                            "persona_source", "limits", "updated"}
    assert written["version"] == 1 and written["persona_source"] == f"git show {SHA}:<path>"
    assert written["updated"] == "2026-10-14T12:00:00+00:00"
    again = run_state.write_state(run_dir, state(stage="submit", stages_done=["select", "implement"], pr=501),
                                  copy_dir, now=NOW)
    assert json.loads((copy_dir / "state.json").read_text())["stage"] == "submit" == again["stage"]
    assert stat.S_IMODE((copy_dir / "state.json").stat().st_mode) == 0o400
    assert run_state.read_state(run_dir) == again and run_state.read_state(tmp_path) is None


def test_write_state_masks_known_secrets(tmp_path):
    written = run_state.write_state(tmp_path, state(slice={"this_pr": f"uses {TOKEN}", "remaining": []}),
                                    tmp_path / "copy", known=[TOKEN])
    assert TOKEN not in json.dumps(written) and TOKEN not in (tmp_path / "copy" / "state.json").read_text()


@pytest.mark.parametrize("change, field", [
    ({"run_id": "today"}, "run_id"), ({"mode": "scheduled"}, "mode"), ({"pinned": "main"}, "pinned"),
    ({"stage": "review"}, "stage"), ({"stages_done": ["probe"]}, "stages_done"), ({"issue": 0}, "issue"),
    ({"branch": "main"}, "branch"), ({"pr": True}, "pr"), ({"head": "x"}, "head"),
    ({"flows": {"flows/a.md": "nope"}}, "flows"), ({"limits": {"max_reviews": 0}}, "limits"),
    ({"limits": {"other": 1}}, "limits"), ({"repository": "IEZhu"}, "repository"),
])
def test_an_invalid_state_is_refused(tmp_path, change, field):
    with pytest.raises(ValueError, match=field):
        run_state.write_state(tmp_path, state(**change), tmp_path / "copy")
    assert not (tmp_path / "state.json").exists()


def test_the_flow_paths_include_the_stage_flows():
    assert set(stages.FLOWS.values()) <= set(run_state.FLOW_PATHS)
