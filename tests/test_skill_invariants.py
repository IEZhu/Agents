"""Mechanical invariants for the skills that carry time-sensitive reference facts.

``test_skill_freshness.py`` enforces the dating contract: that a volatile skill
declares when its figures were read and when they must be read again. This file
enforces the properties of the text itself, the ones a reader cannot check and a
reviewer tends not to.

They are not style rules. Each one failed at least once on real content:

* a source declared as ``http://`` when the publisher serves the same
  consolidated statute over ``https`` — a legal reference should not take its
  figures over a channel a middlebox can rewrite;
* a word drawing letters from two scripts, which looks right and matches
  nothing, so later searches come back empty — and which, for a routing
  keyword, means the alias is dead on arrival;
* an unbalanced ``**`` that swallows the rest of a line into bold;
* a ragged table, which renders with a column silently dropped;
* a date in the future stated as though it were in force, which is how a staged
  reform gets served as current law.

The 2026-11 audit found the first of these in a shipped file and the second in a
correction made while fixing it. The check is cheap and offline; the network-side
check of whether each declared source still serves its document lives in
``scripts/check_skill_sources.py``, which the test suite deliberately does not
run.
"""
from __future__ import annotations

import datetime as dt
import glob
import itertools
import os
import pathlib
import re

import pytest
import yaml

from src.engine.config import AGENTS_DIR, SKILLS_DIR
from src.utils.prompt_loader import split_frontmatter

VOLATILE_PREFIXES = ("skill-jurisdiction-", "skill-labor-", "skill-investment-")

# A word carrying both a Latin and a Cyrillic letter. Georgian, Greek and Serbian
# Cyrillic are all legitimate on their own; the mixture inside one word is not.
SCRIPT_RANGES = (
    ("Latin", ((0x41, 0x5A), (0x61, 0x7A), (0xC0, 0x24F))),
    ("Greek", ((0x370, 0x3FF), (0x1F00, 0x1FFF))),
    ("Cyrillic", ((0x400, 0x52F),)),
    ("Armenian", ((0x530, 0x58F),)),
    ("Hebrew", ((0x590, 0x5FF),)),
    ("Arabic", ((0x600, 0x6FF),)),
    ("Georgian", ((0x10A0, 0x10FF), (0x1C90, 0x1CBF))),
)
WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _scripts(word: str) -> set[str]:
    found = set()
    for char in word:
        code = ord(char)
        for name, ranges in SCRIPT_RANGES:
            if any(low <= code <= high for low, high in ranges):
                found.add(name)
                break
    return found


ROMAN = set("IVXLCDM")


def _is_georgian_act_number(word: str, scripts: set[str]) -> bool:
    """A Georgian act number joins a Roman session numeral to a Georgian letter.

    ``7178-Iს`` and ``3418-IIს`` are how the Georgian legislature numbers its
    acts, so Latin beside Georgian is legitimate there — but only where every
    Latin character is a Roman numeral. ``შromis`` stays a defect.
    """
    return scripts == {"Latin", "Georgian"} and all(
        ch in ROMAN for ch in word if 0x41 <= ord(ch) <= 0x7A
    )


def mixed_script_words(text: str) -> list[str]:
    """Words whose letters come from more than one script: the worst kind of typo.

    It looks right, it matches nothing, and a later search for the term comes
    back empty — which for a routing keyword means the alias is dead.

    An earlier version of this looked only for Latin together with Cyrillic, and
    so missed the defect review found: a Georgian keyword carrying a Cyrillic
    "sha", with no Latin letter in the word at all. A mutation run showed the
    check was narrower than its name.
    """
    found = set()
    for match in WORD.finditer(text):
        word = match.group(0)
        scripts = _scripts(word)
        if len(scripts) > 1 and not _is_georgian_act_number(word, scripts):
            found.add(word)
    return sorted(found)
PLACEHOLDER = re.compile(r"\bTODO\b|\bTBD\b|\bFIXME\b|\bXXX\b")
# Written as escapes on purpose. An earlier version held the characters themselves,
# so a reader could not tell what it matched and an editor or a linter autofix could
# strip them and leave the class empty with nothing failing. Review caught that;
# test_the_invisible_pattern_still_matches_what_it_names now guards it.
INVISIBLE = re.compile(
    "[\u00a0\u200b\u200e\u200f\ufeff]"  # NBSP, ZWSP, LRM, RLM, BOM
)
# A code span is a literal, not prose: `Sentencia T-XXX/YY` is a citation format,
# not an unfinished sentence. Placeholders are looked for outside them.
CODE_SPAN = re.compile(r"`[^`]*`|```.*?```", re.S)
DATE = re.compile(r"\b(\d{2})[.-](\d{2})[.-](20\d{2})\b|\b(20\d{2})-(\d{2})-(\d{2})\b")
# Wording that marks a date as something other than the law in force today.
# Deliberately no bare year: a line stating a 2027 date always contains "2027",
# so admitting the year as its own marker made this check vacuous. A mutation
# test caught that, which is why the pattern is words only.
NOT_YET_IN_FORCE = re.compile(
    r"(from|until|by |end state|phase|staged|expires|review|re-check|re-verify|"
    r"indexed|next|deadline|transitional|not yet|deferred|postponed|effective|"
    r"takes effect|comes into force|commences)",
    re.I,
)


NAMED_INVISIBLES = (0x00A0, 0x200B, 0x200E, 0x200F, 0xFEFF)


def test_the_invisible_pattern_still_matches_what_it_names():
    """Keeps the invisible-character class from being emptied without notice.

    The class is spelled with escapes, but a careless edit could still turn it
    into something that matches nothing — and every file would then pass. This
    asserts the five code points it claims, and that it does not fire on text.
    """
    for code in NAMED_INVISIBLES:
        assert INVISIBLE.search(chr(code)), f"U+{code:04X} no longer matches"
    assert not INVISIBLE.search("ordinary text, 40.70 EUR, art. 14.2")


def _skill_paths() -> list[str]:
    return sorted(
        p
        for p in glob.glob(os.path.join(SKILLS_DIR, "*.mdc"))
        if os.path.basename(p).startswith(VOLATILE_PREFIXES)
    )


def _parts(path: str) -> tuple[dict, str]:
    with open(path, encoding="utf-8") as f:
        fm, body = split_frontmatter(f.read())
    return ((yaml.safe_load(fm) or {}) if fm else {}), body


def _body_line_offset(path: str) -> int:
    """How many lines precede the body, so a report names the line a reader can open."""
    with open(path, encoding="utf-8") as f:
        text = f.read()
    _, body = split_frontmatter(text)
    return text.count("\n") - body.count("\n")


def _pairs() -> list[tuple[str, str]]:
    """Volatile skills that share a country suffix and so compete for the same slots."""
    by_country: dict[str, list[str]] = {}
    for path in _skill_paths():
        name = os.path.basename(path)
        for prefix in VOLATILE_PREFIXES:
            if name.startswith(prefix):
                by_country.setdefault(name[len(prefix) : -len(".mdc")], []).append(path)
                break
    return [pair for paths in by_country.values() for pair in itertools.combinations(sorted(paths), 2)]


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_sources_are_served_over_https(path):
    fm, _ = _parts(path)
    plain = [s for s in (fm.get("sources") or []) if not str(s).startswith("https://")]
    assert not plain, (
        f"{os.path.basename(path)} declares {len(plain)} source(s) over a channel that is not https: "
        f"{plain}. Check whether the publisher serves the same text over https, and say so in the "
        "pull request if it genuinely does not."
    )


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_no_placeholders_or_invisible_characters(path):
    fm, body = _parts(path)
    text = body + "\n" + str(fm.get("compiled", "")) + "\n" + str(fm.get("description", ""))
    prose = CODE_SPAN.sub(" ", text)
    found = PLACEHOLDER.search(prose)
    assert not found, (
        f"{os.path.basename(path)} carries the placeholder {found.group(0)!r} in prose. "
        "A citation-format template belongs in backticks, where this check leaves it alone."
    )
    found = INVISIBLE.search(text)
    assert not found, (
        f"{os.path.basename(path)} contains the invisible character {found.group(0)!r} at offset "
        f"{found.start()}, which breaks later searches for the surrounding term"
    )


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_no_word_mixes_latin_and_cyrillic(path):
    fm, body = _parts(path)
    words = mixed_script_words(body + "\n" + str(fm.get("compiled", "")))
    assert not words, (
        f"{os.path.basename(path)}: {words} draw letters from more than one script in one word. "
        "Write the term in one script; a mixed one matches nothing."
    )


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_emphasis_markers_are_balanced(path):
    _, body = _parts(path)
    off = _body_line_offset(path)
    bad = [i + off for i, line in enumerate(body.split("\n"), 1) if line.count("**") % 2]
    assert not bad, f"{os.path.basename(path)}: unbalanced ** on line(s) {bad}"


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_tables_are_not_ragged(path):
    _, body = _parts(path)
    off = _body_line_offset(path)
    ragged, run = [], []
    for i, line in enumerate(body.split("\n") + [""], 1 + off):
        if line.startswith("|"):
            run.append((i, line.count("|")))
            continue
        if len(run) > 1 and len({w for _, w in run}) > 1:
            ragged.append((run[0][0], sorted({w for _, w in run})))
        run = []
    assert not ragged, (
        f"{os.path.basename(path)}: table(s) starting at line(s) {ragged} have rows of differing "
        "column counts, so a column renders away"
    )


@pytest.mark.parametrize("path", _skill_paths(), ids=os.path.basename)
def test_a_future_date_is_marked_as_not_yet_in_force(path):
    fm, body = _parts(path)
    as_of = fm.get("as_of")
    if not as_of:
        pytest.skip("no as_of to measure against")
    cutoff = as_of if isinstance(as_of, dt.date) else dt.date.fromisoformat(str(as_of))
    offenders = []
    for n, line in enumerate(body.split("\n"), 1 + _body_line_offset(path)):
        for m in DATE.finditer(line):
            g = m.groups()
            try:
                found = (
                    dt.date(int(g[2]), int(g[1]), int(g[0]))
                    if g[0]
                    else dt.date(int(g[3]), int(g[4]), int(g[5]))
                )
            except (TypeError, ValueError):
                continue
            if found > cutoff and not NOT_YET_IN_FORCE.search(line):
                offenders.append((n, m.group(0)))
    assert not offenders, (
        f"{os.path.basename(path)}: {offenders} name a date after as_of {cutoff} without saying it is "
        "not yet in force. A staged reform read as current law is the failure this guards."
    )


def _agent_prompts() -> list[str]:
    return sorted(glob.glob(os.path.join(AGENTS_DIR, "*", "system_prompt.mdc")))


@pytest.mark.parametrize("path", _agent_prompts(), ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_no_routing_keyword_mixes_scripts(path):
    """A routing keyword is matched literally, so a wrong letter makes it dead.

    Found by review, not by this file: a Georgian alias in the labour agent
    carried a Cyrillic "sha" (U+0448) where the Georgian letter (U+10E8)
    belongs, so the keyword could never match the query it existed for. The
    skills were already covered; the agent prompts were not.
    """
    text = pathlib.Path(path).read_text(encoding="utf-8")
    words = mixed_script_words(text)
    assert not words, (
        f"{os.path.basename(os.path.dirname(path))}: {words} draw letters from more than one script "
        "one word. A literal keyword with a letter from the wrong script matches nothing."
    )


@pytest.mark.parametrize("left,right", _pairs(), ids=lambda p: os.path.basename(p))
def test_sibling_skills_share_no_keyword(left, right):
    """Two skills for one country compete for the same two to four retrieval slots.

    A literal shared keyword makes the router pick between them on embedding
    distance alone, which is how a tax question lands in a labour file.
    """
    lk = {str(k).strip().lower() for k in (_parts(left)[0].get("keywords") or [])}
    rk = {str(k).strip().lower() for k in (_parts(right)[0].get("keywords") or [])}
    shared = sorted(lk & rk)
    assert not shared, (
        f"{os.path.basename(left)} and {os.path.basename(right)} both claim {shared}. "
        "Give each its own literals so a query reaches the file that answers it."
    )
