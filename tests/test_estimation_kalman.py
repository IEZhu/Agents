"""The scalar Kalman filter: exact equations, gating, widening and the time-to-threshold."""
import math

import pytest

from src.estimation.kalman import (
    CHI2_1DOF_99,
    SECONDS_PER_DAY,
    Estimate,
    days_until_lower_below,
    innovation,
    predict,
    update,
)

DAY = SECONDS_PER_DAY


def test_chi_square_constant_is_the_99th_percentile_of_one_degree_of_freedom():
    # P(chi2_1 <= x) = erf(sqrt(x / 2)).
    assert math.erf(math.sqrt(CHI2_1DOF_99 / 2)) == pytest.approx(0.99, abs=1e-12)


def test_predict_grows_the_variance_linearly_and_keeps_the_mean():
    start = Estimate(1.5, 0.2, at=0.0)
    later = predict(start, 3 * DAY, q_per_day=0.1)
    assert later.mean == 1.5
    assert later.variance == pytest.approx(0.5)
    assert later.at == 3 * DAY


def test_predict_never_moves_backwards():
    start = Estimate(0.0, 1.0, at=10 * DAY)
    earlier = predict(start, 2 * DAY, q_per_day=1.0)
    assert earlier == start


def test_update_matches_the_textbook_equations():
    prior = Estimate(0.0, 4.0, at=0.0)
    step = update(prior, z=2.0, r=1.0)
    gain = 4.0 / (4.0 + 1.0)
    assert step.gain == pytest.approx(gain)
    assert step.posterior.mean == pytest.approx(gain * 2.0)
    assert step.posterior.variance == pytest.approx((1 - gain) * 4.0)
    assert step.innovation.residual == pytest.approx(2.0)
    assert step.innovation.variance == pytest.approx(5.0)
    assert step.innovation.nis == pytest.approx(4.0 / 5.0)
    assert not step.innovation.gated
    assert step.used_prior == prior


def test_repeated_readings_converge_to_their_value_and_shrink_the_variance():
    estimate = Estimate(0.0, 4.0, at=0.0)
    variances = []
    for _ in range(20):
        estimate = update(estimate, z=3.0, r=1.0).posterior
        variances.append(estimate.variance)
    assert estimate.mean == pytest.approx(3.0, abs=0.2)
    assert all(later < earlier for earlier, later in zip(variances, variances[1:]))


def test_a_surprising_reading_trips_the_gate_and_widens_the_prior():
    prior = Estimate(3.0, 0.1, at=0.0)
    step = update(prior, z=-3.0, r=0.5)
    assert step.innovation.nis == pytest.approx(36.0 / 0.6)
    assert step.innovation.gated
    # Widened until the reading is exactly as surprising as expected: residual**2 = P + r.
    assert step.used_prior.variance == pytest.approx(36.0 - 0.5)
    widened_gain = 35.5 / 36.0
    assert step.posterior.mean == pytest.approx(3.0 + widened_gain * -6.0)
    assert step.posterior.mean < 0
    assert step.posterior.variance > prior.variance


def test_without_widening_a_gated_reading_is_folded_in_plainly():
    prior = Estimate(3.0, 0.1, at=0.0)
    step = update(prior, z=-3.0, r=0.5, widen_on_gate=False)
    assert step.innovation.gated
    assert step.used_prior == prior
    assert step.posterior.mean == pytest.approx(3.0 + (0.1 / 0.6) * -6.0)


def test_innovation_with_zero_variance_is_infinite_only_for_a_nonzero_residual():
    exact = Estimate(1.0, 0.0, at=0.0)
    assert innovation(exact, 1.0, 0.0).nis == 0.0
    assert innovation(exact, 2.0, 0.0).nis == math.inf


def test_days_until_the_lower_bound_crosses_the_threshold():
    estimate = Estimate(3.0, 0.25, at=0.0)
    days = days_until_lower_below(estimate, threshold=1.0, k=1.0, q_per_day=0.05)
    # (3 - 1)**2 = 4 = 0.25 + 0.05 * t  ->  t = 75 days.
    assert days == pytest.approx(75.0)
    crossed = predict(estimate, days * DAY, q_per_day=0.05)
    assert crossed.lower(1.0) == pytest.approx(1.0)


def test_days_until_is_zero_when_already_below_and_none_without_drift():
    assert days_until_lower_below(Estimate(0.5, 1.0, at=0.0), 1.0, 1.0, 0.1) == 0.0
    assert days_until_lower_below(Estimate(3.0, 0.25, at=0.0), 1.0, 1.0, 0.0) is None


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_non_finite_inputs_are_refused(bad):
    with pytest.raises(ValueError):
        Estimate(bad, 1.0, at=0.0)
    with pytest.raises(ValueError):
        update(Estimate(0.0, 1.0, at=0.0), z=bad, r=1.0)


def test_negative_variances_are_refused():
    with pytest.raises(ValueError):
        Estimate(0.0, -1.0, at=0.0)
    with pytest.raises(ValueError):
        predict(Estimate(0.0, 1.0, at=0.0), DAY, q_per_day=-0.1)
    with pytest.raises(ValueError):
        update(Estimate(0.0, 1.0, at=0.0), z=1.0, r=-1.0)


def test_estimate_round_trips_through_a_dict():
    estimate = Estimate(1.25, 0.5, at=12345.0)
    assert Estimate.from_dict(estimate.to_dict()) == estimate
