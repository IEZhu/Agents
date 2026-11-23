"""A drifting success rate from a stream of 0/1 outcomes.

The belief about the success probability ``p`` is kept as ``(mean, variance)`` like
every other estimate here, and is read as the Beta distribution with those two
moments: ``Beta(a, b)`` with ``a + b = n`` and ``variance = mean * (1 - mean) / (n + 1)``.
``n`` is the belief's strength, the number of outcomes it is worth.

An outcome ``y`` in {0, 1} updates the mean in Kalman form,

    mean' = mean + K * (y - mean),    K = variance / (mean * (1 - mean)) = 1 / (n + 1),

which is the gain of a Kalman filter whose reading noise is the expected Bernoulli
variance ``E[p (1 - p)] = mean * (1 - mean) - variance``. That mean is exactly the
conjugate Beta update. The variance is then the Beta posterior's,
``mean' * (1 - mean') / (n + 2)``, rather than the linear filter's ``(1 - K) * variance``:
on the probability scale the linear update shrinks the variance more slowly than the
spread ``mean * (1 - mean)`` near 0 and 1, the gain climbs towards 1 and a long run of
successes drives the mean to exactly 1, where it can never learn again.

Drift is fading memory: between outcomes the strength decays with a half-life,
``n(t + dt) = n(t) * 2 ** (-dt / half_life)``, never below the uniform prior's 2, so an
outcome counts half as much after one half-life and the mean is unchanged by time
alone. An additive process noise would not do here: the same added variance is a
small widening at 0.5 and a collapse of all evidence at 0.99.
"""
from __future__ import annotations

from src.estimation.kalman import Estimate, SECONDS_PER_DAY

# Beta(1, 1), the uniform prior: strength 2, mean 1/2, variance 1/12.
PRIOR_STRENGTH = 2.0
UNIFORM_MEAN = 0.5
# The normal quantile of a two-sided 95 percent band.
Z95 = 1.959963984540054


def prior(at: float) -> Estimate:
    return _moments(UNIFORM_MEAN, PRIOR_STRENGTH, at)


def _moments(mean: float, strength: float, at: float) -> Estimate:
    return Estimate(mean, mean * (1.0 - mean) / (strength + 1.0), at)


def strength(estimate: Estimate) -> float:
    """The number of outcomes the belief is worth; infinite for a belief with no variance."""
    spread = estimate.mean * (1.0 - estimate.mean)
    if estimate.variance <= 0.0 or spread <= 0.0:
        return float("inf")
    return max(spread / estimate.variance - 1.0, 0.0)


def forward(estimate: Estimate, at: float, half_life_days: float) -> Estimate:
    """Carry the belief to ``at``: its strength fades, its mean does not move."""
    if half_life_days <= 0:
        raise ValueError(f"half_life_days must be positive, got {half_life_days!r}")
    elapsed = max(0.0, at - estimate.at)
    current = strength(estimate)
    if current == float("inf") or elapsed == 0.0:
        return Estimate(estimate.mean, estimate.variance, max(at, estimate.at))
    faded = current * 2.0 ** (-(elapsed / SECONDS_PER_DAY) / half_life_days)
    # Time never makes a belief weaker than the uniform prior, nor stronger than it was.
    return _moments(estimate.mean, max(min(PRIOR_STRENGTH, current), faded), max(at, estimate.at))


def observe(estimate: Estimate, success: bool, at: float, half_life_days: float) -> Estimate:
    """Carry the belief to ``at`` and fold in one outcome."""
    current = forward(estimate, at, half_life_days)
    n = strength(current)
    if n == float("inf"):
        return current  # a belief with no variance cannot learn; only a constructed one is like that
    gain = 1.0 / (n + 1.0)
    target = 1.0 if success else 0.0
    mean = current.mean + gain * (target - current.mean)
    return _moments(mean, n + 1.0, current.at)


def band(estimate: Estimate, z: float = Z95) -> tuple[float, float]:
    """``mean -+ z * std`` clipped to ``[0, 1]``: a normal approximation, rough for few outcomes."""
    return max(0.0, estimate.mean - z * estimate.std), min(1.0, estimate.mean + z * estimate.std)
