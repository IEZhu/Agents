"""Wiring and internal consistency of the data skills.

Four skills carry narrow knowledge the data personas lacked: an input audit
before computing on tables, clinical laboratory units, statistical geography
and the SEC Form 13F data sets. Two failures are cheap to catch from the files alone and expensive
to notice any other way:

* a skill no agent lists is never loaded, however good its text, and a skill
  an agent lists but that does not exist is dropped with only a log warning;
* the lab-unit table states a conversion factor, an interval in conventional
  units and the same interval in SI units, so an edit to one column can leave
  the others teaching a wrong number with full confidence. The standard tier
  renders only the ``compiled`` line, so a factor that drifts between the
  table and that line is wrong for half the readers.

The checks read frontmatter and Markdown only and need nothing beyond the
packages the content-checks workflow installs.
"""
from __future__ import annotations

import glob
import os
import re

import pytest
import yaml

from src.engine.config import AGENTS_DIR, SKILLS_DIR
from src.utils.prompt_loader import split_frontmatter

DATA_SKILLS = (
    "skill-data-intake-audit",
    "skill-clinical-lab-units",
    "skill-statistical-geography",
    "skill-sec-13f-holdings",
)

# (agent, skill list, skill): the attachment each skill was added for.
WIRING = (
    ("data_analyst", "core_skills", "skill-data-intake-audit"),
    ("data_forensic", "preferred_skills", "skill-data-intake-audit"),
    ("data_analyst", "capable_skills", "skill-clinical-lab-units"),
    ("medical_expert", "capable_skills", "skill-clinical-lab-units"),
    ("data_analyst", "capable_skills", "skill-statistical-geography"),
    ("data_analyst", "capable_skills", "skill-sec-13f-holdings"),
)
SKILL_LISTS = ("core_skills", "preferred_skills", "capable_skills")
CORE_BUDGET = 3


def _parts(path: str) -> tuple[dict, str]:
    with open(path, encoding="utf-8") as f:
        fm, body = split_frontmatter(f.read())
    return ((yaml.safe_load(fm) or {}) if fm else {}), body


def _skill(name: str) -> tuple[dict, str]:
    return _parts(os.path.join(SKILLS_DIR, f"{name}.mdc"))


def _agent_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(AGENTS_DIR, "*", "system_prompt.mdc")))


def _agent(name: str) -> dict:
    return _parts(os.path.join(AGENTS_DIR, name, "system_prompt.mdc"))[0]


@pytest.mark.parametrize("skill", DATA_SKILLS)
def test_skill_carries_retrieval_metadata(skill):
    fm, body = _skill(skill)
    for field in ("description", "compiled"):
        assert isinstance(fm.get(field), str) and fm[field].strip(), f"{skill}: empty {field}"
    keywords = fm.get("keywords")
    assert isinstance(keywords, list) and 5 <= len(keywords) <= 12, (
        f"{skill}: keywords must be a list of 5 to 12 phrases, got {keywords!r}"
    )
    assert all(isinstance(k, str) and k.strip() for k in keywords), f"{skill}: blank keyword"
    assert "## Rules" in body and "## Concepts" in body, f"{skill}: missing a Rules or Concepts section"


@pytest.mark.parametrize("skill", DATA_SKILLS)
def test_keywords_are_not_claimed_by_another_skill(skill):
    """A keyword shared with another skill boosts both, and they then compete for the same slots."""
    own = {k.lower().strip() for k in _skill(skill)[0]["keywords"]}
    for path in sorted(glob.glob(os.path.join(SKILLS_DIR, "*.mdc"))):
        other = os.path.basename(path)[: -len(".mdc")]
        if other == skill:
            continue
        theirs = {str(k).lower().strip() for k in (_parts(path)[0].get("keywords") or [])}
        assert not own & theirs, f"{skill} shares keywords {sorted(own & theirs)} with {other}"


@pytest.mark.parametrize(("agent", "field", "skill"), WIRING)
def test_skill_is_wired_to_its_agent(agent, field, skill):
    assert skill in (_agent(agent).get(field) or []), f"{agent}.{field} no longer lists {skill}"


@pytest.mark.parametrize("path", _agent_paths(), ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_every_listed_skill_exists(path):
    fm = _parts(path)[0]
    missing = [
        name
        for field in SKILL_LISTS
        for name in (fm.get(field) or [])
        if not os.path.isfile(os.path.join(SKILLS_DIR, f"{name}.mdc"))
    ]
    assert not missing, f"{path} lists skills that do not exist: {missing}"


@pytest.mark.parametrize("path", _agent_paths(), ids=lambda p: os.path.basename(os.path.dirname(p)))
def test_core_skills_stay_within_budget(path):
    """Core skills load on every tier, lite included; the schema allows at most three."""
    core = _parts(path)[0].get("core_skills") or []
    assert len(core) <= CORE_BUDGET, f"{path} has {len(core)} core skills: {core}"


NUMBER = re.compile(r"\d+(?:\.\d+)?")


def _bounds(cell: str) -> tuple[float | None, float]:
    """``"0.6–1.2"`` -> (0.6, 1.2); ``"under 200"`` -> (None, 200); trailing words are ignored."""
    numbers = NUMBER.findall(cell)
    if cell.strip().startswith("under"):
        return None, float(numbers[0])
    return float(numbers[0]), float(numbers[1])


def _tolerances(cell: str) -> list[float]:
    """Half a unit of the last digit shown for each number, so rounding for display is allowed."""
    return [
        0.5 * 10 ** -(len(n.split(".")[1]) if "." in n else 0) + 1e-9
        for n in NUMBER.findall(cell)
    ]


LAB_COLUMNS = 8
SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")


def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(cells: list[str]) -> bool:
    return all(SEPARATOR_CELL.match(c) for c in cells)


def _lab_table() -> list[list[str]]:
    """Every line of the table that starts at the `| Analyte` header, split into cells."""
    _, body = _skill("skill-clinical-lab-units")
    lines = body.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("| Analyte")), None)
    if start is None:
        return []
    table = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        table.append(_cells(line))
    return table


def _lab_rows() -> list[list[str]]:
    """The data rows: everything after the header that is not a separator.

    Nothing else is filtered out, and the separator is recognised by its own
    cells rather than by position, so a missing separator cannot take the first
    data row with it. A row with the wrong number of cells reaches the checks
    below and fails them, instead of silently escaping all of them.
    """
    return [cells for cells in _lab_table()[1:] if not _is_separator(cells)]


def test_lab_table_was_parsed():
    table = _lab_table()
    assert table, "no `| Analyte` table found in skill-clinical-lab-units; update this parser with it"
    assert len(table) > 1 and _is_separator(table[1]), (
        "the line after the `| Analyte` header is not a Markdown separator, so the table does not render"
    )
    rows = _lab_rows()
    assert rows, "the lab-unit table has no data rows"
    ragged = [row[0] for row in rows if len(row) != LAB_COLUMNS]
    assert not ragged, f"lab-table rows without {LAB_COLUMNS} cells, so no check below covers them: {ragged}"


@pytest.mark.parametrize("row", _lab_rows(), ids=lambda r: r[0])
def test_lab_si_interval_equals_conventional_times_factor(row):
    analyte, _conv_unit, _si_unit, factor_text, conventional, si, window, _separates = row
    factor = float(factor_text)
    conv_low, conv_high = _bounds(conventional)
    si_low, si_high = _bounds(si)
    tolerances = _tolerances(si)
    pairs = [(conv_high, si_high, tolerances[-1])]
    if conv_low is not None:
        pairs.append((conv_low, si_low, tolerances[0]))
    for conv_value, si_value, tolerance in pairs:
        expected = conv_value * factor
        assert abs(expected - si_value) <= tolerance, (
            f"{analyte}: {conv_value} x {factor} = {expected:.4g}, table says {si_value}"
        )


@pytest.mark.parametrize("row", _lab_rows(), ids=lambda r: r[0])
def test_lab_window_contains_typical_interval(row):
    analyte, *_, si, window, _separates = row
    si_low, si_high = _bounds(si)
    window_low, window_high = _bounds(window)
    assert window_high >= si_high, f"{analyte}: window {window} cuts off the typical interval {si}"
    if si_low is not None:
        assert window_low <= si_low, f"{analyte}: window {window} cuts off the typical interval {si}"


# How `compiled` names each table row's analyte. Several rows share a factor
# (x10, x0.0259, x0.25), so finding the number somewhere in the line proves
# nothing: the analyte has to be named in the stretch of text that leads up to
# that factor. A row added to the table without an entry here fails below.
COMPILED_ANALYTE = {
    "Creatinine, serum": "creatinine",
    "Creatinine, urine (spot)": "urine creatinine",
    "Urea from BUN": "bun",
    "Urea": "urea",
    "Phosphate (as phosphorus)": "phosphate",
    "Magnesium": "magnesium",
    "Calcium, total": "calcium",
    "Calcium, ionized": "calcium",
    "Cholesterol, total": "cholesterol",
    "HDL cholesterol": "cholesterol",
    "LDL cholesterol": "cholesterol",
    "Triglycerides": "triglycerides",
    "Glucose": "glucose",
    "Total protein": "total protein",
    "Albumin": "albumin",
    "Prealbumin (transthyretin)": "prealbumin",
    "Uric acid": "uric acid",
    "Bilirubin, total": "bilirubin",
}
FACTOR_TOKEN = re.compile(r"(?<![\w.])x(\d+(?:\.\d+)?)(?![\d.]*\d)")


def _compiled_factors() -> list[tuple[str, str]]:
    """Each factor token in `compiled` with the text between it and the previous one."""
    compiled = _skill("skill-clinical-lab-units")[0]["compiled"]
    pairs, previous_end = [], 0
    for match in FACTOR_TOKEN.finditer(compiled):
        pairs.append((match.group(1), compiled[previous_end : match.start()].lower()))
        previous_end = match.end()
    return pairs


def _names(text: str, analyte: str) -> bool:
    """The analyte as a whole word or phrase, so `albumin` does not match inside `prealbumin`."""
    return re.search(rf"(?<![a-z]){re.escape(analyte)}(?![a-z])", text) is not None


@pytest.mark.parametrize("row", _lab_rows(), ids=lambda r: r[0])
def test_lab_factor_matches_compiled_line(row):
    analyte, factor = row[0], row[3]
    assert analyte in COMPILED_ANALYTE, f"{analyte}: add how `compiled` names it to COMPILED_ANALYTE"
    name = COMPILED_ANALYTE[analyte]
    leads = [lead for token, lead in _compiled_factors() if token == factor]
    assert leads, f"{analyte}: factor {factor} is in the table but not in `compiled`, which is all the standard tier shows"
    assert any(_names(lead, name) for lead in leads), (
        f"{analyte}: `compiled` gives x{factor}, but not after naming {name!r}; "
        "the standard tier would teach this factor for another analyte"
    )
