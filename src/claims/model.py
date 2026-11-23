"""How a claim's verdicts become a confidence: a scalar Kalman filter on log-odds.

The state is the log-odds that the claim holds now. Each verdict is a reading of it:
a check at verification level ``V1``..``V4`` that confirms the claim reads
``+logit(accuracy)``, one that refutes it reads ``-logit(accuracy)``, with a reading
variance per level. Between verdicts the state drifts as a random walk whose
variance per day is the claim's volatility, so confidence decays while nobody looks
and a fact has to be checked again. The filter is ``src.estimation.kalman``.

Two properties follow from treating verdicts as readings of one quantity rather than
as independent evidence, and both are deliberate:

* Confidence converges to what the strongest routes used can support and does not
  compound past it. Verifiers built on similar models fail together (the plan's
  coincident-failure measurement, z = 29.2), so ten agreeing checks of one kind are
  not ten independent proofs. More checks narrow the band instead, which is what
  lets a fact stay reusable for longer.
* A verdict far from the belief trips the innovation gate. The reading is still
  applied, after widening the prior so it is consistent (``kalman.update``), and the
  claim is ``contested`` until a later verdict agrees with the belief it left behind.
  A contradiction is a trigger for fresh validation, never a verdict by itself.

Every number below is a prior chosen for this design, not a measured accuracy of
any route; docs/validated-claims.md says how to replace them once the ledger
measures route accuracy (plan layer 8).
"""
from __future__ import annotations

from dataclasses import dataclass
import math

from src.estimation import kalman

LEVEL_ORDER = ("V1", "V2", "V3", "V4")
IMPORTANCE_ORDER = ("routine", "load_bearing", "critical")
VOLATILITY_ORDER = ("stable", "normal", "volatile")
VERDICTS = ("confirmed", "refuted")


@dataclass(frozen=True)
class Reading:
    """What one verdict at a level reads: ``+-logit(accuracy)`` with variance ``variance``."""

    accuracy: float
    variance: float

    @property
    def magnitude(self) -> float:
        return math.log(self.accuracy / (1.0 - self.accuracy))


@dataclass(frozen=True)
class Need:
    """What reuse at an importance level requires: a confirming level and a lower bound."""

    level: str
    lower: float


# The thread-close verification levels. V0 (the model's words only) validates nothing
# and is not accepted.
READINGS = {
    "V1": Reading(accuracy=0.80, variance=0.50),  # a source was read and supports it
    "V2": Reading(accuracy=0.90, variance=0.25),  # a command, test or call produced it
    "V3": Reading(accuracy=0.95, variance=0.20),  # an independent fresh-context check
    "V4": Reading(accuracy=0.98, variance=0.15),  # the owner accepted it and it held
}
# The importance levels of plan layer 7, with the level each needs (none for routine,
# one route-different check for load_bearing, a blinded independent one for critical)
# and the lower confidence bound reuse requires.
NEEDS = {
    "routine": Need(level="V1", lower=0.60),
    "load_bearing": Need(level="V2", lower=0.80),
    "critical": Need(level="V3", lower=0.90),
}
# Random-walk variance of the log-odds per day without a verdict.
VOLATILITY = {
    "stable": 0.002,   # a published standard, a released API: months between checks
    "normal": 0.02,    # code and configuration in an active repository: weeks
    "volatile": 0.2,   # counts, states, open pull requests, anything that moves daily
}
# The belief before any verdict: even odds, two log-odds units either way.
PRIOR_MEAN = 0.0
PRIOR_VARIANCE = 9.0
# The band is mean -+ BAND_K standard deviations; reuse compares its lower edge.
BAND_K = 1.0
GATE = kalman.CHI2_1DOF_99


def logistic(x: float) -> float:
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    e = math.exp(x)
    return e / (1.0 + e)


def logit(p: float) -> float:
    return math.log(p / (1.0 - p))


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

    estimate: kalman.Estimate
    state: str
    level: str | None
    importance: str
    usable_for: tuple[str, ...]
    revalidate_at: float | None
    contested: bool
    last_nis: float
    checks: int
    confirmed: int
    refuted: int
    last_verdict: str
    last_at: float
    as_of: float

    @property
    def confidence(self) -> float:
        return logistic(self.estimate.mean)

    @property
    def lower(self) -> float:
        return logistic(self.estimate.lower(BAND_K))

    @property
    def upper(self) -> float:
        return logistic(self.estimate.upper(BAND_K))


def _at_least(level: str | None, needed: str) -> bool:
    return level is not None and LEVEL_ORDER.index(level) >= LEVEL_ORDER.index(needed)


def _usable(estimate: kalman.Estimate, level: str | None, contested: bool) -> tuple[str, ...]:
    if contested:
        return ()
    lower = logistic(estimate.lower(BAND_K))
    return tuple(name for name in IMPORTANCE_ORDER
                 if _at_least(level, NEEDS[name].level) and lower >= NEEDS[name].lower)


def assess(verdicts: list[Verdict], now: float) -> Assessment:
    """Filter ``verdicts`` (oldest first) and carry the result to ``now``."""
    if not verdicts:
        raise ValueError("a claim has at least one verdict")
    estimate = kalman.Estimate(PRIOR_MEAN, PRIOR_VARIANCE, verdicts[0].at)
    volatility = verdicts[0].volatility
    level: str | None = None
    importance = "routine"
    contested = False
    nis = 0.0
    confirmed = refuted = 0
    for verdict in verdicts:
        prior = kalman.predict(estimate, verdict.at, VOLATILITY[volatility])
        reading = READINGS[verdict.level]
        sign = 1.0 if verdict.verdict == "confirmed" else -1.0
        step = kalman.update(prior, sign * reading.magnitude, reading.variance, gate=GATE)
        estimate = step.posterior
        contested = step.innovation.gated
        nis = step.innovation.nis
        volatility = verdict.volatility
        if IMPORTANCE_ORDER.index(verdict.importance) > IMPORTANCE_ORDER.index(importance):
            importance = verdict.importance
        if sign > 0:
            confirmed += 1
            if not _at_least(level, verdict.level):
                level = verdict.level
        else:
            refuted += 1
            level = None  # only confirmations since the last refutation count towards reuse
    settled = estimate
    q = VOLATILITY[volatility]
    current = kalman.predict(estimate, now, q)
    lower = logistic(current.lower(BAND_K))
    upper = logistic(current.upper(BAND_K))
    supported_floor = NEEDS["routine"].lower
    if contested:
        state = "contested"
    elif lower >= supported_floor:
        state = "supported"
    elif upper <= 1.0 - supported_floor:
        state = "refuted"
    elif logistic(settled.lower(BAND_K)) >= supported_floor:
        state = "stale"
    else:
        state = "uncertain"
    # A due date fixed at the last verdict, the same whenever it is read: the moment the band's
    # lower edge, widening from there, falls below what the claim's importance needs.
    need = NEEDS[importance]
    if contested or not _at_least(level, need.level):
        revalidate_at = settled.at
    else:
        days = kalman.days_until_lower_below(settled, logit(need.lower), BAND_K, q)
        revalidate_at = None if days is None else settled.at + days * kalman.SECONDS_PER_DAY
    last = verdicts[-1]
    return Assessment(estimate=current, state=state, level=level, importance=importance,
                      usable_for=_usable(current, level, contested), revalidate_at=revalidate_at,
                      contested=contested, last_nis=nis, checks=len(verdicts), confirmed=confirmed,
                      refuted=refuted, last_verdict=last.verdict, last_at=last.at, as_of=current.at)
