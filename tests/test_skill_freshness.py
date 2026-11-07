"""Freshness contract for skills that carry time-sensitive reference facts.

Skills are injected into the system prompt, where the model reads them as
authoritative context. A tax rate or visa threshold that was right when the
skill was written keeps being served long after the law changes, and the
no-fabrication rule does not catch it because the figure is "in context".
(The 2026-09 audit found outdated figures in 7 of the 9 jurisdiction skills.)

Contract:
  * a skill with volatile facts declares ``as_of`` (ISO date), ``review_after_days``
    and ``sources`` in its frontmatter;
  * jurisdiction, labour and investment skills must declare them;
  * a skill whose figures are reset on a known calendar date — a statutory
    minimum wage or a contribution rate that changes every 1 January — also
    declares ``review_on`` (ISO date), the next date on which they must be
    re-read. The rolling window alone would keep serving last year's figures
    for months after the reset;
  * once the earlier of ``as_of + review_after_days`` and ``review_on`` has
    passed, this test fails, which forces a re-verification instead of letting
    the figures silently age.

[flows/legal-reference-refresh.md](../flows/legal-reference-refresh.md) is the
procedure that clears a failure here.
"""
from __future__ import annotations

import datetime as dt
import glob
import os

import pytest
import yaml

from src.engine.config import SKILLS_DIR
from src.utils.prompt_loader import split_frontmatter

MUST_DECLARE_FRESHNESS = ("skill-jurisdiction-", "skill-labor-", "skill-investment-")


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


def _due_date(fm: dict) -> tuple[dt.date, str]:
    """The earlier of the rolling window and the declared calendar re-check."""
    as_of = _parse_date(fm["as_of"])
    rolling = as_of + dt.timedelta(days=int(fm.get("review_after_days", 180)))
    if not fm.get("review_on"):
        return rolling, "review_after_days"
    pinned = _parse_date(fm["review_on"])
    return (pinned, "review_on") if pinned < rolling else (rolling, "review_after_days")


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
    [p for p in _skill_files() if _frontmatter(p).get("review_on")],
    ids=os.path.basename,
)
def test_review_on_is_a_date_after_as_of(path):
    fm = _frontmatter(path)
    assert fm.get("as_of"), f"{os.path.basename(path)}: review_on without as_of"
    review_on = _parse_date(fm["review_on"])
    as_of = _parse_date(fm["as_of"])
    assert review_on > as_of, (
        f"{os.path.basename(path)}: review_on {review_on} is not after as_of {as_of}; "
        "it names the next date the figures must be re-read, not a past one."
    )


@pytest.mark.parametrize(
    "path",
    [p for p in _skill_files() if "as_of" in _frontmatter(p)],
    ids=os.path.basename,
)
def test_declared_facts_are_not_past_review(path):
    fm = _frontmatter(path)
    due, field = _due_date(fm)
    assert dt.date.today() <= due, (
        f"{os.path.basename(path)}: facts as_of {fm['as_of']} were due for re-verification "
        f"on {due} ({field}). Re-check the figures against the listed sources, then bump "
        "as_of and review_on. flows/legal-reference-refresh.md is the procedure."
    )


# The parametrized tests above exercise `_due_date` only through the files in
# skills/, and every one of those is currently inside both windows. The branch
# that matters most — a pinned calendar date that falls due while the rolling
# window is still open — is therefore never reached by them. These unit cases
# pin the arithmetic itself, with fixed dates, so they do not rot.
@pytest.mark.parametrize(
    ("fm", "due", "field"),
    [
        # no review_on: the rolling window decides
        ({"as_of": "2026-11-06", "review_after_days": 180}, dt.date(2027, 5, 5), "review_after_days"),
        # review_on earlier than the rolling window: review_on decides
        (
            {"as_of": "2026-11-06", "review_after_days": 180, "review_on": "2027-02-15"},
            dt.date(2027, 2, 15),
            "review_on",
        ),
        # review_on later than the rolling window: the window still decides
        (
            {"as_of": "2026-11-06", "review_after_days": 180, "review_on": "2027-12-31"},
            dt.date(2027, 5, 5),
            "review_after_days",
        ),
        # review_after_days omitted: the 180-day default applies
        ({"as_of": "2026-11-06", "review_on": "2027-02-15"}, dt.date(2027, 2, 15), "review_on"),
        # a YAML date object and a stringified count are both accepted
        ({"as_of": dt.date(2026, 11, 6), "review_after_days": "180"}, dt.date(2027, 5, 5), "review_after_days"),
    ],
)
def test_due_date_takes_the_earlier_of_the_two_dates(fm, due, field):
    assert _due_date(fm) == (due, field)


def test_a_past_review_on_falls_due_while_the_rolling_window_is_still_open():
    """The scenario review_on exists for: without it the figures age silently."""
    fm = {"as_of": "2026-06-01", "review_after_days": 180, "review_on": "2026-11-01"}
    due, field = _due_date(fm)
    rolling = _parse_date(fm["as_of"]) + dt.timedelta(days=fm["review_after_days"])
    assert field == "review_on"
    assert due == dt.date(2026, 11, 1)
    assert rolling > due, "the rolling window must still be open, or the case proves nothing"


def test_review_on_equal_to_the_rolling_window_keeps_the_window_as_the_reason():
    fm = {"as_of": "2026-11-06", "review_after_days": 180, "review_on": "2027-05-05"}
    assert _due_date(fm) == (dt.date(2027, 5, 5), "review_after_days")
