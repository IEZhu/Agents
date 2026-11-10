"""The separator rule the contract document states has one source (#596)."""
import re
import textwrap
from pathlib import Path

import pytest

from src.engine import subagent_contract_text as contract_text
from src.engine import subagent_text
from src.engine.subagent_text import SEPARATORS, WRITE_SEPARATOR, Separator, separator_rule

ROOT = Path(__file__).resolve().parents[1]
MOVED = Separator(" => ", re.compile(r"\s+=>\s+"), write_it=True, note="a fat arrow")
# Narrow enough that a wrap has to decide inside a quoted spelling, wide enough to
# include the document's own width.
WIDTHS = tuple(range(40, 101, 2))


@pytest.fixture
def moved_source(monkeypatch):
    """The written spelling moved, as a contributor editing ``SEPARATORS`` would move it."""
    kept = tuple(separator for separator in SEPARATORS if not separator.write_it)
    monkeypatch.setattr(subagent_text, "SEPARATORS", (MOVED, *kept))
    monkeypatch.setattr(subagent_text, "WRITE_SEPARATOR", MOVED)
    return MOVED


def _quoted(block):
    """The block as one line, without its quote marks, for comparing against the source."""
    return " ".join(line.removeprefix(contract_text.QUOTE) for line in block.splitlines())


def test_the_committed_block_is_the_render():
    stale = [str(path.relative_to(ROOT)) for path in contract_text.stale_files()]
    assert not stale, f"stale {stale}: run {contract_text.REGENERATE}"


def test_the_block_is_the_sentences_the_package_states():
    """The document quotes ``separator_rule``, wrapped, rather than paraphrasing it."""
    block = contract_text.separator_block()
    assert _quoted(block) == " ".join(separator_rule())
    assert all(line.startswith(contract_text.QUOTE) for line in block.splitlines())
    assert max(len(line) for line in block.splitlines()) <= contract_text.WIDTH


def _split_spellings(block):
    """The quoted spellings a line break broke: whole occurrences missing from the lines."""
    flat = _quoted(block)
    return [f"'{separator.spelling}'" for separator in SEPARATORS
            if sum(line.count(f"'{separator.spelling}'") for line in block.splitlines())
            != flat.count(f"'{separator.spelling}'")]


@pytest.mark.parametrize("width", WIDTHS)
def test_no_width_splits_a_spelling(width):
    """A break between the quote and the dash would show a spelling nobody writes.

    Every width, not only the document's: which one splits depends on the sentences, and
    they are edited. At the document's width today none would, so a test of that width
    alone passes with the masking removed.
    """
    assert _split_spellings(contract_text.separator_block(width)) == []


def test_the_masking_is_load_bearing():
    """Keeps the test above honest: some width in its range splits a spelling unprotected."""
    flat = " ".join(separator_rule())
    unprotected = [width for width in WIDTHS
                   if _split_spellings(textwrap.fill(
                       flat, width=width, initial_indent=contract_text.QUOTE,
                       subsequent_indent=contract_text.QUOTE,
                       break_long_words=False, break_on_hyphens=False))]
    assert unprotected, "no width in WIDTHS splits a spelling unprotected; widen the range"


def test_moving_the_source_moves_the_block_and_makes_the_document_stale(moved_source):
    """The point of the exercise: a rule edited in the engine cannot leave the document behind."""
    block = contract_text.separator_block()
    assert f"'{moved_source.spelling}'" in _quoted(block)
    assert WRITE_SEPARATOR.spelling not in _quoted(block)
    assert contract_text.stale_files() == [contract_text.DOCUMENT]


def test_writing_is_idempotent_and_reports_only_a_real_change(moved_source, tmp_path, monkeypatch):
    document = tmp_path / "subagent-contract.md"
    document.write_text(f"before\n{contract_text.BEGIN_MARKER}\nstale\n"
                        f"{contract_text.END_MARKER}\nafter\n", encoding="utf-8")
    monkeypatch.setattr(contract_text, "DOCUMENT", document)
    assert contract_text.write_generated() == [document]
    assert contract_text.write_generated() == []
    text = document.read_text(encoding="utf-8")
    assert text.startswith("before\n") and text.endswith("\nafter\n")
    assert f"'{moved_source.spelling}'" in text and "stale" not in text


def test_a_document_without_a_marker_pair_is_refused(tmp_path, monkeypatch):
    document = tmp_path / "subagent-contract.md"
    document.write_text(f"{contract_text.BEGIN_MARKER}\nno end marker\n", encoding="utf-8")
    monkeypatch.setattr(contract_text, "DOCUMENT", document)
    with pytest.raises(contract_text.ContractTextError, match="marker pair"):
        contract_text.stale_files()


def test_the_command_reports_a_stale_block_and_writes_it(moved_source, tmp_path, monkeypatch, capsys):
    document = tmp_path / "subagent-contract.md"
    document.write_text(f"{contract_text.BEGIN_MARKER}\n{contract_text.END_MARKER}\n", encoding="utf-8")
    monkeypatch.setattr(contract_text, "DOCUMENT", document)
    monkeypatch.setattr(contract_text, "ROOT", tmp_path)
    assert contract_text.main(["--check"]) == 1
    assert contract_text.REGENERATE in capsys.readouterr().err
    assert contract_text.main(["--write"]) == 0
    assert "updated subagent-contract.md" in capsys.readouterr().out
    assert contract_text.main(["--check"]) == 0
