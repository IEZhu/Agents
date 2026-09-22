"""Freshness contract for skills that carry time-sensitive reference facts.

Skills are injected into the system prompt, where the model reads them as
authoritative context. A tax rate or visa threshold that was right when the
skill was written keeps being served long after the law changes, and the
no-fabrication rule does not catch it because the figure is "in context".
(The 2026-09 audit found outdated figures in 7 of the 9 jurisdiction skills.)

Contract:
  * a skill with volatile facts declares ``as_of`` (ISO date), ``review_after_days``
    and ``sources`` in its frontmatter;
  * jurisdiction skills must declare them;
  * once ``as_of + review_after_days`` has passed, this test fails, which forces
    a re-verification instead of letting the figures silently age.
"""
from __future__ import annotations

import datetime as dt
import glob
import os

import pytest
import yaml

from src.engine.config import SKILLS_DIR
from src.utils.prompt_loader import split_frontmatter

MUST_DECLARE_FRESHNESS = ("skill-jurisdiction-",)


def _frontmatter(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        fm, _ = split_frontmatter(f.read())
    return (yaml.safe_load(fm) or {}) if fm else {}


def _skill_files() -> list[str]:
    return sorted(glob.glob(os.path.join(SKILLS_DIR, "*.mdc")))


def _parse_date(raw) -> dt.date:
    if isinstance(raw, dt.date):
        return raw
    return dt.date.fromisoformat(str(raw))


@pytest.mark.parametrize(
    "path",
    [p for p in _skill_files() if os.path.basename(p).startswith(MUST_DECLARE_FRESHNESS)],
    ids=os.path.basename,
)
def test_volatile_skills_declare_freshness(path):
    fm = _frontmatter(path)
    missing = [k for k in ("as_of", "review_after_days", "sources") if not fm.get(k)]
    assert not missing, f"{os.path.basename(path)} lacks freshness fields: {missing}"
    assert isinstance(fm["sources"], list) and all(isinstance(s, str) for s in fm["sources"])


@pytest.mark.parametrize(
    "path",
    [p for p in _skill_files() if "as_of" in _frontmatter(p)],
    ids=os.path.basename,
)
def test_declared_facts_are_not_past_review(path):
    fm = _frontmatter(path)
    as_of = _parse_date(fm["as_of"])
    review_days = int(fm.get("review_after_days", 180))
    due = as_of + dt.timedelta(days=review_days)
    assert dt.date.today() <= due, (
        f"{os.path.basename(path)}: facts as_of {as_of} were due for re-verification on {due}. "
        "Re-check the figures against the listed sources, then bump as_of."
    )
