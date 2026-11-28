"""The turn-log hook's records for user sync, on two machines that share a project's history (#701).

The Stop hook (``src.memory.turn_log_hook``) shares nothing itself: it records the entries it replaces
or withdraws, so the next reconcile of a process that shares the repository's history takes them out
of what this machine shared. These tests run the hook against the two machines and the local bare
repository of tests/test_user_sync_history.py. The hook needs the runtime's dependencies (pydantic,
numpy), which the User sync workflow does not install, so they live in their own file, which the
Workspace bindings workflow runs on Linux, macOS and Windows with requirements.txt installed (#701
review: in tests/test_user_sync_history.py they failed that workflow on every system).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src.memory import history as journal
from src.user_sync import history as history_sync
from tests.test_user_sync import remote  # noqa: F401 (fixture)
from tests.test_user_sync_history import KEY, Box, clock, isolated_sync, shared, two  # noqa: F401 (fixtures)


@pytest.fixture
def hook_process(monkeypatch):
    """The Stop hook runs as its own process, which shares nothing itself: it finds this machine's sync
    state where a server finds it, and reads its transcript without waiting."""
    from src.memory import turn_log_hook as hook
    monkeypatch.setattr(hook, "TRANSCRIPT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(hook, "TRANSCRIPT_POLL_SECONDS", 0.01)
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return hook


def as_this_machine(monkeypatch, box: Box) -> None:
    """Where the hook process finds the sync state and library: the defaults a server uses."""
    monkeypatch.setattr(history_sync.engine, "default_state_dir", lambda: box.machine.state)
    monkeypatch.setattr(history_sync.engine, "default_library", lambda: box.machine.lib)


def hook_stop(box: Box, transcript: Path, **extra) -> dict:
    return {"session_id": "5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10", "transcript_path": str(transcript),
            "cwd": str(box.repo), "hook_event_name": "Stop", "stop_hook_active": False, "prompt_id": "p1", **extra}


def reconcile(box: Box) -> dict:
    """What a server process of this machine runs after any append, at its start or on resume."""
    return history_sync.reconcile(box.repo, state_dir=box.machine.state, library=box.machine.lib)


def own_segments(box: Box) -> str:
    return "".join(text for name, text in box.segments().items() if name.startswith(box.label + "/"))


def test_a_corrected_answer_the_hook_wrote_after_a_reconcile_replaces_the_shared_one(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """r5a: a server reconciled the checkout between the two Stops of one turn. The hook records the
    entry it replaced, so the next reconcile shares the corrected answer and takes the first one out,
    and the other machine reads the corrected answer."""
    from tests.test_turn_log_hook import CORRECTED, FEEDBACK, LONG_ANSWER, activation_rows, answer_row, flat, opener, \
        rows_file
    a, b = shared
    as_this_machine(monkeypatch, a)
    assert journal._sync is None  # the hook process: no sync of its own
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(),
                                                  answer_row(LONG_ANSWER, "m1"), FEEDBACK, answer_row(CORRECTED, "m2")])
    first = hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=LONG_ANSWER), interactive=True)
    assert first["status"] == "recorded" and reconcile(a)["status"] == "exported"
    again = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=CORRECTED),
                                     interactive=True)
    assert again["replaced"]["entry_id"] == first["entry_id"] == again["entry_id"]
    assert history_sync._read_state(a.machine.state)["deleted"] == {KEY: [[first["entry_id"], first["timestamp"]]]}
    outcome = reconcile(a)
    assert (outcome["removed"], outcome["written"]) == ([first["entry_id"]], [again["entry_id"]]), outcome
    assert reconcile(a)["status"] == "unchanged"
    assert flat(CORRECTED) in own_segments(a) and flat(LONG_ANSWER) not in own_segments(a)
    for box in (a, b, a, b):
        box.sync()
    assert [entry.outcome for entry in b.read(limit=20) if entry.intent == "question"] == [flat(CORRECTED)]
    assert [entry.outcome for entry in a.read(limit=20) if entry.intent == "question"] == [flat(CORRECTED)]


def test_an_entry_the_hook_withdrew_after_a_reconcile_leaves_the_shared_history(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """r5b: another Stop hook demanded log_interaction, whose append made the server's worker share the
    checkout while the hook's entry of the turn was in it. The hook records its withdrawal, so merged
    reads leave that entry out at once and the next reconcile takes it out of what this machine shared."""
    from tests.test_turn_log_hook import FEEDBACK, LONG_ANSWER, activation_rows, answer_row, flat, log_rows, opener, \
        rows_file
    a, b = shared
    as_this_machine(monkeypatch, a)
    rows = [opener("p1", "question"), *activation_rows(), answer_row(LONG_ANSWER, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    first = hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=LONG_ANSWER), interactive=True)
    model = "The model's own redacted text of the same turn."
    assert a.log("question", model)["status"] == "recorded"  # the server appends and its worker reconciles
    assert flat(LONG_ANSWER) in own_segments(a)
    rows_file(transcript, [*rows, FEEDBACK, *log_rows({"status": "recorded"}), answer_row("Logged.", "m2")])
    gone = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message="Logged."),
                                    interactive=True)
    assert gone == {"status": "skipped", "reason": "logged_by_model", "withdrawn": True}
    assert [entry.outcome for entry in a.read(limit=20) if entry.intent == "question"] == [model]  # at once
    assert reconcile(a)["removed"] == [first["entry_id"]]
    assert flat(LONG_ANSWER) not in own_segments(a) and model in own_segments(a)
    for box in (a, b, a, b):
        box.sync()
    assert [entry.outcome for entry in b.read(limit=20) if entry.intent == "question"] == [model]


def hook_turn(tmp_path, prompt_id="p1", question="question"):
    """A transcript whose turn delivered LONG_ANSWER, then CORRECTED after another Stop hook continued it."""
    from tests.test_turn_log_hook import CORRECTED, FEEDBACK, LONG_ANSWER, activation_rows, answer_row, opener, rows_file
    return rows_file(tmp_path / f"{prompt_id}.jsonl", [
        opener(prompt_id, question), *activation_rows(), answer_row(LONG_ANSWER, "m1"),
        {**FEEDBACK, "promptId": prompt_id}, answer_row(CORRECTED, "m2")]), LONG_ANSWER, CORRECTED


def question_outcomes(box: Box, question="question") -> list[str]:
    return [entry.outcome for entry in box.read(limit=50) if entry.intent == question]


def test_a_corrected_answer_written_in_the_same_second_still_replaces_the_shared_one(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """Final verification T4: with both writes of a turn in one second, the record of the replaced entry
    named its replacement too, so the next reconcile took the first answer out and shared nothing."""
    from tests.test_turn_log_hook import flat
    a, b = shared
    as_this_machine(monkeypatch, a)
    transcript, first_answer, corrected = hook_turn(tmp_path)
    frozen = clock.now
    first = hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=first_answer), interactive=True)
    assert reconcile(a)["status"] == "exported"
    clock.now = frozen  # the second Stop's write lands in the same second as the first
    again = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                     interactive=True)
    assert again["timestamp"] > first["timestamp"] and again["entry_id"] == first["entry_id"]
    outcome = reconcile(a)
    assert (outcome["removed"], outcome["written"]) == ([first["entry_id"]], [again["entry_id"]]), outcome
    for box in (a, b, a, b):
        box.sync()
    assert question_outcomes(b) == [flat(corrected)] and question_outcomes(a) == [flat(corrected)]


def no_space_left(*_arguments, **_keywords):
    import errno
    raise OSError(errno.ENOSPC, "No space left on device")


def cancelled(*_arguments, **_keywords):
    raise KeyboardInterrupt("the client cancelled the hook after its record")


def replace_failing_for(text, failure):
    """``journal._replace`` that fails as ``failure`` does for the write holding ``text``."""
    from tests.test_turn_log_hook import flat
    real_replace = journal._replace

    def replace(path, content, *, info):
        if flat(text) in flat(content):
            failure()
        return real_replace(path, content, info=info)
    return replace


@pytest.mark.parametrize("failure", [no_space_left, cancelled], ids=["full disk", "interrupted"])
def test_a_journal_change_that_raises_after_its_record_takes_the_record_back(
        shared, clock, hook_process, monkeypatch, tmp_path, failure):
    """v701-4 regression (s1b): the removal is recorded before the journal changes, and a replacement that
    then failed left a record of an entry the journal still held: the next reconcile took the turn out of
    the shared history for good. The record is taken back now, so both machines keep the first answer
    until the turn's next Stop replaces it."""
    from tests.test_turn_log_hook import flat
    a, b = shared
    as_this_machine(monkeypatch, a)
    transcript, first_answer, corrected = hook_turn(tmp_path)
    first = hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=first_answer), interactive=True)
    assert reconcile(a)["status"] == "exported"
    real_replace = journal._replace
    monkeypatch.setattr(journal, "_replace", replace_failing_for(corrected, failure))
    with pytest.raises((OSError, KeyboardInterrupt)):
        hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                 interactive=True)
    monkeypatch.setattr(journal, "_replace", real_replace)
    assert not (history_sync._read_state(a.machine.state).get("deleted") or {}).get(KEY)
    assert reconcile(a)["status"] == "unchanged"
    for box in (a, b, a, b):
        box.sync()
    assert question_outcomes(a) == [flat(first_answer)] and question_outcomes(b) == [flat(first_answer)]
    again = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                     interactive=True)
    outcome = reconcile(a)
    assert "replaced" in again and (outcome["removed"], outcome["written"]) == ([first["entry_id"]], [again["entry_id"]])
    for box in (a, b, a, b):
        box.sync()
    assert question_outcomes(b) == [flat(corrected)]


def test_a_hook_killed_inside_the_journal_change_keeps_its_record_until_the_turns_next_stop(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """Final verification T6, the window docs/user-sync.md names: a hook killed inside the journal's
    replacement (about a millisecond), after its record, cannot take the record back. The next reconcile
    takes the shared copy out, so nothing stale stays shared, and the turn's next Stop shares the corrected
    answer; without one the other machine reads no entry of the turn. The kill is simulated by an
    interruption whose take-back never runs."""
    from tests.test_turn_log_hook import flat
    a, b = shared
    as_this_machine(monkeypatch, a)
    transcript, first_answer, corrected = hook_turn(tmp_path)
    first = hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=first_answer), interactive=True)
    assert reconcile(a)["status"] == "exported"
    for box in (a, b):
        box.sync()
    assert question_outcomes(b) == [flat(first_answer)]
    real_replace, real_not_deleting = journal._replace, hook_process._RemovalsForSync.not_deleting
    monkeypatch.setattr(journal, "_replace", replace_failing_for(corrected, cancelled))
    monkeypatch.setattr(hook_process._RemovalsForSync, "not_deleting", lambda *arguments: None)  # never reached
    with pytest.raises(KeyboardInterrupt):
        hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                 interactive=True)
    monkeypatch.setattr(journal, "_replace", real_replace)
    monkeypatch.setattr(hook_process._RemovalsForSync, "not_deleting", real_not_deleting)
    assert history_sync._read_state(a.machine.state)["deleted"] == {KEY: [[first["entry_id"], first["timestamp"]]]}
    assert reconcile(a)["removed"] == [first["entry_id"]]
    for box in (a, b, a, b):
        box.sync()
    assert question_outcomes(b) == []  # the first answer left the shared history; nothing stale remains
    again = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                     interactive=True)
    assert "replaced" in again and reconcile(a)["written"] == [again["entry_id"]]
    for box in (a, b, a, b):
        box.sync()
    assert question_outcomes(b) == [flat(corrected)]


def test_the_hook_resolves_the_checkout_before_it_takes_the_journal_lock(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """v701-4 info: the record ran two git calls under the journal's write locks (17 of 18 ms), keeping
    other writers waiting; the hook resolves the checkout's key before them now."""
    import src.user_flows
    a, _b = shared
    as_this_machine(monkeypatch, a)
    transcript, first_answer, corrected = hook_turn(tmp_path)
    hook_process.record_turn(hook_stop(a, transcript, last_assistant_message=first_answer), interactive=True)
    history_sync._WORKSPACES.clear()
    real_repo_key, held = src.user_flows.repo_key, []
    monkeypatch.setattr(src.user_flows, "repo_key", lambda root: held.append(journal._WRITE_LOCK.locked())
                        or real_repo_key(root))
    again = hook_process.record_turn(hook_stop(a, transcript, stop_hook_active=True, last_assistant_message=corrected),
                                     interactive=True)
    assert "replaced" in again
    assert held and not any(held), held
    assert history_sync._read_state(a.machine.state)["deleted"][KEY]


def test_an_unchanged_reconcile_after_many_replaced_turns_does_not_call_the_writer(
        shared, clock, hook_process, monkeypatch, tmp_path):
    """Final verification T5: the record of a replaced turn named an id still shared (its replacement's),
    so every later reconcile called the writer and tried to cut the entry out again, record by record."""
    a, _b = shared
    as_this_machine(monkeypatch, a)
    turns = []
    for index in range(20):
        transcript, first_answer, corrected = hook_turn(tmp_path, f"p{index}", f"question {index}")
        hook_process.record_turn(hook_stop(a, transcript, prompt_id=f"p{index}", last_assistant_message=first_answer),
                                 interactive=True)
        turns.append((transcript, corrected))
    assert reconcile(a)["status"] == "exported"
    for index, (transcript, corrected) in enumerate(turns):
        again = hook_process.record_turn(hook_stop(a, transcript, prompt_id=f"p{index}", stop_hook_active=True,
                                                   last_assistant_message=corrected), interactive=True)
        assert "replaced" in again
    applied = reconcile(a)
    assert len(applied["removed"]) == len(applied["written"]) == 20, applied
    assert len(history_sync._read_state(a.machine.state)["deleted"][KEY]) == 20
    calls = []
    real_write = history_sync._write
    monkeypatch.setattr(history_sync, "_write", lambda *arguments, **keywords: calls.append(1) or real_write(
        *arguments, **keywords))
    assert reconcile(a)["status"] == "unchanged" and calls == []


