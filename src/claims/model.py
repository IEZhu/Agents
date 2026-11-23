"""How a claim's verdicts become a confidence: the exact two-state Bayes filter.

A claim is true or false now, and it can change by a jump: the code it describes is
edited, the service it describes is reconfigured. Its verdicts come from routes that
fail together when they share a cause, so the filter tracks, next to the claim's
truth ``theta``, one hidden reading ``z_f`` per family of routes: what that family
as a whole would say about the claim now. A route reads its family's reading with
its own accuracy ``b``; the family reads the truth with its common-mode accuracy
``c``. The belief is a distribution over the 2 ** (families + 1) combinations of
``theta`` and the readings, and the filter is the forward recursion of that hidden
Markov model:

* predict: the truth switches at ``lambda`` per day in either direction (the
  claim's volatility), and at every switch the readings are drawn afresh, each
  correct with probability ``c``. Over ``dt`` days a combination keeps its readings
  only if no switch happened, ``exp(-lambda * dt)``; otherwise the readings are
  fresh for whichever truth the switches ended on. The kernel is exact for any
  split of time: two predicts of seven days equal one of fourteen;
* update: a verdict multiplies every combination by ``b`` when it agrees with its
  family's reading and by ``1 - b`` when it does not, then the belief is normalised.

It is exact under its model. Two consequences are the point of it:

* Agreeing verdicts of one family cannot push the claim's log-odds above
  ``logit(c)`` of that family, however many there are: verifiers built on similar
  models fail together (the plan's coincident-failure measurement, z = 29.2).
  Confidence beyond that needs a second, independent family.
* A verdict that the belief made unlikely, predictive probability below
  ``1 / (1 + GATE)``, is a contradiction: the claim is ``contested`` until a later
  verdict is not surprising. A contradiction is a reason to validate again, never a
  verdict by itself; the update itself is exact and needs no special case.

The Kalman filter in ``src.estimation`` is the Gaussian special case of the same
predict-update recursion. For a binary claim it was measured worse: on 3000
simulated claims the Brier score was 0.0109 for this filter, 0.0136 for a Kalman
filter on the indicator scale and 0.025 to 0.047 for one on log-odds readings.

Every number below is a prior chosen for this design, not a measured accuracy;
docs/validated-claims.md says how to replace them once the ledger measures route
accuracy (plan layer 8).
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import product
import math

SECONDS_PER_DAY = 86_400.0
LEVEL_ORDER = ("V1", "V2", "V3", "V4")
IMPORTANCE_ORDER = ("routine", "load_bearing", "critical")
VOLATILITY_ORDER = ("stable", "normal", "volatile")
VERDICTS = ("confirmed", "refuted")


@dataclass(frozen=True)
class Route:
    """A verification level: the family it belongs to and its own accuracy ``b``."""

    family: str
    accuracy: float


@dataclass(frozen=True)
class Need:
    """What reuse at an importance level requires: a confirming level and a confidence."""

    level: str
    confidence: float


# Families of routes that fail together, with their common-mode accuracy c.
FAMILIES = {
    "model": 0.97,       # a language model reading a source or reviewing: V1 and V3 share it
    "execution": 0.98,   # a command, test or call whose output was seen: V2
    "owner": 0.99,       # the owner accepted it and it held: V4
}
FAMILY_ORDER = tuple(FAMILIES)
# The thread-close verification levels. V0 (the model's own words) validates nothing.
ROUTES = {
    "V1": Route(family="model", accuracy=0.85),      # a source was read and supports it
    "V2": Route(family="execution", accuracy=0.95),  # a command, test or call produced it
    "V3": Route(family="model", accuracy=0.97),      # an independent fresh-context check
    "V4": Route(family="owner", accuracy=0.99),      # the owner accepted it and it held
}
# The importance levels of plan layer 7: the level each needs (one route-different
# check for load_bearing, an independent one for critical) and the confidence reuse needs.
NEEDS = {
    "routine": Need(level="V1", confidence=0.75),
    "load_bearing": Need(level="V2", confidence=0.85),
    "critical": Need(level="V3", confidence=0.95),
}
# Rate per day at which the claim's truth switches, in either direction. Confidence relaxes
# towards 1/2 with a half-life of ln 2 / (2 * rate): about a year, six weeks and two days.
VOLATILITY = {
    "stable": 1.0 / 730.0,   # a published standard, a released API
    "normal": 1.0 / 120.0,   # a constant, a signature or a setting in an active repository
    "volatile": 1.0 / 7.0,   # counts, states, open pull requests, anything that moves daily
}
PRIOR = 0.5
# The 0.99 quantile of chi-square with one degree of freedom, the plan's gate. A verdict
# whose predictive probability is below 1 / (1 + GATE), about 0.131, is a contradiction.
GATE = 6.634896601021214
SURPRISE = 1.0 / (1.0 + GATE)
# A claim is decided either way past this confidence: supported above, refuted below 1 - it.
DECIDED = NEEDS["routine"].confidence

Cell = tuple[int, ...]  # (theta, z_model, z_execution, z_owner)
_CELLS: tuple[Cell, ...] = tuple(product((1, 0), repeat=len(FAMILY_ORDER) + 1))


def route_accuracy(level: str) -> float:
    """One verdict's accuracy on its own: ``c * b + (1 - c) * (1 - b)``."""
    route = ROUTES[level]
    c = FAMILIES[route.family]
    return c * route.accuracy + (1.0 - c) * (1.0 - route.accuracy)


def _fresh(theta: int) -> dict[Cell, float]:
    """The readings given the truth: each family reads it right with probability ``c``."""
    weights = {}
    for cell in _CELLS:
        if cell[0] != theta:
            continue
        weight = 1.0
        for index, family in enumerate(FAMILY_ORDER, start=1):
            c = FAMILIES[family]
            weight *= c if cell[index] == theta else 1.0 - c
        weights[cell] = weight
    return weights


def prior_belief(p: float = PRIOR) -> dict[Cell, float]:
    belief = {cell: p * weight for cell, weight in _fresh(1).items()}
    belief.update({cell: (1.0 - p) * weight for cell, weight in _fresh(0).items()})
    return belief


def truth(belief: dict[Cell, float]) -> float:
    """``P(theta = 1)``: the claim's confidence."""
    return sum(weight for cell, weight in belief.items() if cell[0] == 1)


def predict(belief: dict[Cell, float], days: float, rate: float) -> dict[Cell, float]:
    """Carry the belief ``days`` forward; the truth switches at ``rate`` per day either way.

    With ``k`` switches in the interval a Poisson count: none keeps the combination as
    it is (``exp(-rate * days)``); an odd number ends on the other truth, an even
    number of at least two on the same one, and either way with fresh readings.
    """
    if days <= 0:
        return dict(belief)
    none = math.exp(-rate * days)
    odd = 0.5 * (1.0 - math.exp(-2.0 * rate * days))
    even_after_switching = 1.0 - odd - none
    true_mass = truth(belief)
    false_mass = 1.0 - true_mass
    fresh_true, fresh_false = _fresh(1), _fresh(0)
    carried = {}
    for cell, weight in belief.items():
        if cell[0] == 1:
            arriving = fresh_true[cell] * (true_mass * even_after_switching + false_mass * odd)
        else:
            arriving = fresh_false[cell] * (false_mass * even_after_switching + true_mass * odd)
        carried[cell] = weight * none + arriving
    return carried


def _likelihood(cell: Cell, confirmed: bool, level: str) -> float:
    route = ROUTES[level]
    reading = cell[1 + FAMILY_ORDER.index(route.family)]
    return route.accuracy if (reading == 1) == confirmed else 1.0 - route.accuracy


def update(belief: dict[Cell, float], confirmed: bool, level: str) -> tuple[dict[Cell, float], float]:
    """Fold in one verdict: the new belief and the verdict's predictive probability."""
    weighted = {cell: weight * _likelihood(cell, confirmed, level) for cell, weight in belief.items()}
    evidence = sum(weighted.values())
    return {cell: weight / evidence for cell, weight in weighted.items()}, evidence


def aged(p: float, days: float, rate: float) -> float:
    """``P(true)`` after ``days`` without a verdict: it relaxes towards 1/2."""
    return 0.5 + (p - 0.5) * math.exp(-2.0 * rate * max(0.0, days))


def days_until_below(p: float, threshold: float, rate: float) -> float | None:
    """Days until ``aged(p)`` falls to ``threshold`` (above 1/2); 0 if it is already there.

    From ``aged``: ``t = ln((p - 1/2) / (threshold - 1/2)) / (2 * rate)``.
    """
    if p <= threshold:
        return 0.0
    if rate <= 0 or threshold <= 0.5:
        return None  # it only approaches 1/2, so a bound at or below 1/2 is never crossed
    return math.log((p - 0.5) / (threshold - 0.5)) / (2.0 * rate)


@dataclass(frozen=True)
class Verdict:
    """One recorded verdict, as the filter needs it."""

    at: float
    verdict: str
    level: str
    volatility: str
    importance: str


@dataclass(frozen=True)
class Assessment:
    """The filtered state of one claim as of ``as_of``."""

    confidence: float
    settled: float
    state: str
    level: str | None
    families: tuple[str, ...]
    importance: str
    usable_for: tuple[str, ...]
    revalidate_at: float | None
    contested: bool
    surprise: float
    checks: int
    confirmed: int
    refuted: int
    last_verdict: str
    last_at: float
    as_of: float


def _at_least(level: str | None, needed: str) -> bool:
    return level is not None and LEVEL_ORDER.index(level) >= LEVEL_ORDER.index(needed)


def assess(verdicts: list[Verdict], now: float) -> Assessment:
    """Filter ``verdicts`` (oldest first) and carry the result to ``now``."""
    if not verdicts:
        raise ValueError("a claim has at least one verdict")
    verdicts = sorted(verdicts, key=lambda verdict: verdict.at)  # stable: same-time verdicts keep their order
    belief = prior_belief()
    at = verdicts[0].at
    volatility = verdicts[0].volatility
    level: str | None = None
    families: list[str] = []
    importance = "routine"
    contested = False
    predictive = 1.0
    confirmed = refuted = 0
    for verdict in verdicts:
        belief = predict(belief, (verdict.at - at) / SECONDS_PER_DAY, VOLATILITY[volatility])
        at = max(at, verdict.at)
        agrees = verdict.verdict == "confirmed"
        belief, predictive = update(belief, agrees, verdict.level)
        contested = predictive < SURPRISE
        volatility = verdict.volatility
        if IMPORTANCE_ORDER.index(verdict.importance) > IMPORTANCE_ORDER.index(importance):
            importance = verdict.importance
        if agrees:
            confirmed += 1
            if not _at_least(level, verdict.level):
                level = verdict.level
            family = ROUTES[verdict.level].family
            if family not in families:
                families.append(family)
        else:
            refuted += 1
            level, families = None, []  # only confirmations since the last refutation count for reuse
    rate = VOLATILITY[volatility]
    settled = truth(belief)
    now = max(now, at)
    confidence = aged(settled, (now - at) / SECONDS_PER_DAY, rate)
    if contested:
        state = "contested"
    elif confidence >= DECIDED:
        state = "supported"
    elif confidence <= 1.0 - DECIDED:
        state = "refuted"
    elif settled >= DECIDED or settled <= 1.0 - DECIDED:
        state = "stale"  # decided at its last verdict; time alone moved it out
    else:
        state = "uncertain"
    usable = () if contested else tuple(
        name for name in IMPORTANCE_ORDER
        if _at_least(level, NEEDS[name].level) and confidence >= NEEDS[name].confidence)
    # A due date fixed at the last verdict, the same whenever it is read.
    need = NEEDS[importance]
    if contested or not _at_least(level, need.level):
        revalidate_at = at
    else:
        days = days_until_below(settled, need.confidence, rate)
        revalidate_at = None if days is None else at + days * SECONDS_PER_DAY
    last = verdicts[-1]
    return Assessment(confidence=confidence, settled=settled, state=state, level=level,
                      families=tuple(families), importance=importance, usable_for=usable,
                      revalidate_at=revalidate_at, contested=contested, surprise=predictive,
                      checks=len(verdicts), confirmed=confirmed, refuted=refuted,
                      last_verdict=last.verdict, last_at=last.at, as_of=now)
