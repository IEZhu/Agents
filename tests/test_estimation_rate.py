"""The drifting success rate: the conjugate Beta update in Kalman form, fading memory and the band."""
import pytest

from src.estimation import rate
from src.estimation.kalman import SECONDS_PER_DAY, Estimate

DAY = SECONDS_PER_DAY


def observe_all(outcomes, half_life_days=1e9, day_step=0.0):
    estimate = rate.prior(0.0)
    for index, success in enumerate(outcomes):
        estimate = rate.observe(estimate, success, index * day_step * DAY, half_life_days)
    return estimate


def test_without_fading_the_belief_is_exactly_the_conjugate_beta_posterior():
    outcomes = [True, True, False, True, True, True, False, True, True, True, True, False]
    estimate = observe_all(outcomes)
    a = 1 + sum(outcomes)
    b = 1 + len(outcomes) - sum(outcomes)
    assert estimate.mean == pytest.approx(a / (a + b))
    assert estimate.variance == pytest.approx(a * b / ((a + b) ** 2 * (a + b + 1)))
    assert rate.strength(estimate) == pytest.approx(a + b)


def test_the_gain_is_the_kalman_gain_with_expected_bernoulli_noise():
    estimate = observe_all([True, False, True])
    spread = estimate.mean * (1 - estimate.mean)
    r = spread - estimate.variance
    after = rate.observe(estimate, True, 0.0, 1e9)
    gain = estimate.variance / (estimate.variance + r)
    assert after.mean == pytest.approx(estimate.mean + gain * (1 - estimate.mean))


def test_a_long_run_of_successes_never_reaches_one():
    estimate = observe_all([True] * 5000, half_life_days=30, day_step=0.1)
    assert estimate.mean < 1.0
    assert rate.observe(estimate, False, 501 * DAY, 30).mean < estimate.mean


def test_the_mean_stays_inside_the_unit_interval_for_any_stream():
    estimate = rate.prior(0.0)
    for index in range(300):
        estimate = rate.observe(estimate, index % 7 != 0, index * DAY, half_life_days=3)
        assert 0.0 < estimate.mean < 1.0
        assert rate.strength(estimate) >= rate.PRIOR_STRENGTH


def test_fading_lets_a_new_regime_replace_an_old_one():
    still = observe_all([True] * 100 + [False] * 20)
    fading = observe_all([True] * 100 + [False] * 20, half_life_days=7, day_step=1.0)
    assert fading.mean < 0.5 < still.mean


def test_forward_fades_the_strength_by_its_half_life_and_keeps_the_mean():
    estimate = observe_all([True, False] * 20)  # strength 42
    later = rate.forward(estimate, 10 * DAY, half_life_days=10)
    assert later.mean == pytest.approx(estimate.mean)
    assert rate.strength(later) == pytest.approx(21.0)
    far = rate.forward(estimate, 10_000 * DAY, half_life_days=10)
    assert rate.strength(far) == pytest.approx(rate.PRIOR_STRENGTH)


def test_forward_refuses_a_non_positive_half_life():
    with pytest.raises(ValueError):
        rate.forward(rate.prior(0.0), DAY, half_life_days=0)


def test_band_is_clipped_to_the_unit_interval():
    low, high = rate.band(Estimate(0.97, 0.01, at=0.0))
    assert low == pytest.approx(0.97 - rate.Z95 * 0.1)
    assert high == 1.0


def test_a_degenerate_belief_does_not_move():
    certain = Estimate(1.0, 0.0, at=0.0)
    assert rate.observe(certain, False, DAY, half_life_days=1).mean == 1.0
