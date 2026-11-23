"""Scalar Kalman filter on a local-level (random-walk) model.

The tracked quantity ``x`` drifts as a random walk and is read with noise:

    x(t + dt) = x(t) + w,  w ~ N(0, q * dt)        (process: q is variance per day)
    z         = x(t) + v,  v ~ N(0, r)             (measurement)

The belief about ``x`` is a Gaussian ``N(mean, variance)``. ``predict`` carries it
forward in time, which only widens it; ``update`` folds in one reading. Because the
model is linear and Gaussian these are the exact Kalman equations, and the filter
needs nothing beyond the standard library.

Every reading also yields its innovation: the residual ``z - mean`` and its variance
``S = variance + r``. The normalised innovation squared ``NIS = residual**2 / S``
follows a chi-square distribution with one degree of freedom when the model holds,
so a reading whose NIS exceeds a chi-square quantile is inconsistent with what the
filter believed. Callers use that gate to notice a contradiction, not to settle it:
``update`` can either fold such a reading in plainly or first widen the prior until
the reading is consistent with it, so that a real change of the quantity is
absorbed quickly and the lost confidence stays visible in the variance.

Time is seconds since the epoch, as ``time.time()`` returns it. A reading older than
the estimate is applied at the estimate's time: the filter never moves backwards.
"""
from __future__ import annotations

from dataclasses import dataclass
import math

SECONDS_PER_DAY = 86_400.0

# The 0.99 quantile of chi-square with one degree of freedom: a reading consistent
# with the belief exceeds it about once in a hundred readings.
CHI2_1DOF_99 = 6.634896601021214


def _finite(name: str, value: float) -> float:
    value = float(value)
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    return value


def _non_negative(name: str, value: float) -> float:
    value = _finite(name, value)
    if value < 0:
        raise ValueError(f"{name} must not be negative, got {value!r}")
    return value


@dataclass(frozen=True)
class Estimate:
    """A Gaussian belief ``N(mean, variance)`` about the quantity, as of ``at``."""

    mean: float
    variance: float
    at: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "mean", _finite("mean", self.mean))
        object.__setattr__(self, "variance", _non_negative("variance", self.variance))
        object.__setattr__(self, "at", _finite("at", self.at))

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    def lower(self, k: float) -> float:
        """``mean - k * std``: the lower edge of the band ``k`` standard deviations wide."""
        return self.mean - k * self.std

    def upper(self, k: float) -> float:
        return self.mean + k * self.std

    def to_dict(self) -> dict:
        return {"mean": self.mean, "variance": self.variance, "at": self.at}

    @classmethod
    def from_dict(cls, data: dict) -> "Estimate":
        return cls(mean=data["mean"], variance=data["variance"], at=data["at"])


@dataclass(frozen=True)
class Innovation:
    """How far a reading fell from the prediction, and whether that was surprising."""

    residual: float
    variance: float
    nis: float
    gated: bool


@dataclass(frozen=True)
class Step:
    """One update: the predicted prior, the widened prior actually used, and the posterior."""

    prior: Estimate
    used_prior: Estimate
    posterior: Estimate
    innovation: Innovation
    gain: float


def predict(estimate: Estimate, at: float, q_per_day: float) -> Estimate:
    """Carry the belief forward to ``at``; the variance grows by ``q_per_day`` per day."""
    q_per_day = _non_negative("q_per_day", q_per_day)
    at = _finite("at", at)
    elapsed = max(0.0, at - estimate.at)
    return Estimate(estimate.mean, estimate.variance + q_per_day * elapsed / SECONDS_PER_DAY,
                    max(at, estimate.at))


def innovation(prior: Estimate, z: float, r: float, *, gate: float = CHI2_1DOF_99) -> Innovation:
    """The innovation of reading ``z`` with variance ``r`` against ``prior``, gated at ``gate``."""
    z = _finite("z", z)
    r = _non_negative("r", r)
    gate = _non_negative("gate", gate)
    residual = z - prior.mean
    variance = prior.variance + r
    if variance == 0:
        nis = 0.0 if residual == 0 else math.inf
    else:
        nis = residual * residual / variance
    return Innovation(residual=residual, variance=variance, nis=nis, gated=nis > gate)


def update(prior: Estimate, z: float, r: float, *, gate: float = CHI2_1DOF_99,
           widen_on_gate: bool = True) -> Step:
    """Fold reading ``z`` (variance ``r``) into ``prior``.

    When the reading trips the gate and ``widen_on_gate`` is set, the prior variance
    is first raised to ``residual**2 - r``, the variance at which the reading would
    have been expected (its NIS becomes exactly 1). The posterior then moves most of
    the way to the reading and keeps a wide variance, instead of either ignoring a
    real change or pretending to be as sure as before it.
    """
    measured = innovation(prior, z, r, gate=gate)
    used = prior
    if measured.gated and widen_on_gate:
        widened = measured.residual * measured.residual - float(r)
        if widened > prior.variance:
            used = Estimate(prior.mean, widened, prior.at)
    total = used.variance + float(r)
    gain = used.variance / total if total > 0 else 0.0
    posterior = Estimate(used.mean + gain * measured.residual, (1.0 - gain) * used.variance, used.at)
    return Step(prior=prior, used_prior=used, posterior=posterior, innovation=measured, gain=gain)


def days_until_lower_below(estimate: Estimate, threshold: float, k: float, q_per_day: float) -> float | None:
    """Days until ``mean - k * std`` falls to ``threshold`` while no reading arrives.

    Under the random walk the mean stays put and the variance grows linearly, so the
    crossing solves ``mean - k * sqrt(variance + q * t) = threshold``:

        t = (((mean - threshold) / k) ** 2 - variance) / q

    Returns 0 when the bound is already at or below the threshold, and ``None`` when
    it never gets there (no process noise, or ``k`` is 0 with the mean above it).
    """
    threshold = _finite("threshold", threshold)
    k = _non_negative("k", k)
    q_per_day = _non_negative("q_per_day", q_per_day)
    if estimate.lower(k) <= threshold:
        return 0.0
    if q_per_day == 0 or k == 0:
        return None
    margin = (estimate.mean - threshold) / k
    return max(0.0, (margin * margin - estimate.variance) / q_per_day)
