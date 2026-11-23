"""How long retrieval warmup takes on this installation, learned across starts.

Every successful warmup reports its total duration. The typical duration is tracked
with a scalar Kalman filter (``src.estimation.kalman``) on the logarithm of the
seconds, because durations are positive and their scatter is multiplicative: a
cold model cache, a larger index or a busy machine stretch a start by a factor, not
by a fixed amount. The estimate drifts between starts, so a machine that got faster
or slower is followed instead of averaged over its whole history.

The estimate only feeds the ``warming_up`` hint, so a caller knows whether to retry
in two seconds or in a minute. It never changes ``WARMUP_WAIT_SECONDS`` or any other
bound. State lives in the installation's ``data/readiness/warmup-estimate.json``;
every read or write failure is logged and the hint falls back to its plain text.
"""
from __future__ import annotations

import json
import logging
import math
import os
from pathlib import Path
import time

from src.estimation import kalman
from src.file_lock import file_lock

logger = logging.getLogger(__name__)

# Chosen priors, not measurements. One start's log-duration scatters around the
# typical one with this variance (a standard deviation of 0.5: about a factor of
# 1.65 either way), and the typical one drifts by this much per day.
READING_VARIANCE = 0.25
DRIFT_PER_DAY = 0.05
# With no history: about ten seconds, anything from about 1.4 to 74 seconds plausible.
PRIOR_MEAN = math.log(10.0)
PRIOR_VARIANCE = 4.0
# The hint's "up to" is the one-sided 90 percent edge of the typical duration.
UPPER_K = 1.2815515655446004
FORMAT_VERSION = 1


def state_path() -> Path:
    from src.engine import config  # lazy, like the ledger: tests redirect the data directory

    return Path(config.INSTALL_DATA_DIR) / "readiness" / "warmup-estimate.json"


def _load(path: Path) -> tuple[kalman.Estimate, int] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("v") != FORMAT_VERSION:
            return None
        return kalman.Estimate.from_dict(data["estimate"]), int(data["starts"])
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as error:
        logger.warning("Warmup estimate unreadable at %s: %s", path, error)
        return None


def record(total_seconds: float, now: float | None = None) -> None:
    """Fold one successful warmup's duration into the stored estimate; never raises."""
    if not (isinstance(total_seconds, (int, float)) and math.isfinite(total_seconds) and total_seconds > 0):
        return
    now = time.time() if now is None else now
    path = state_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with file_lock(path.with_name(path.name + ".lock")):
            loaded = _load(path)
            estimate, starts = loaded if loaded else (kalman.Estimate(PRIOR_MEAN, PRIOR_VARIANCE, now), 0)
            prior = kalman.predict(estimate, now, DRIFT_PER_DAY)
            # A first start that downloads the model is an outlier: fold it in with the ordinary
            # gain rather than widening, so one slow start moves the estimate only part of the way.
            step = kalman.update(prior, math.log(total_seconds), READING_VARIANCE, widen_on_gate=False)
            payload = {"v": FORMAT_VERSION, "estimate": step.posterior.to_dict(), "starts": starts + 1}
            temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(temporary, path)
    except OSError as error:
        logger.warning("Warmup estimate not saved at %s: %s", path, error)


def typical(now: float | None = None) -> tuple[float, float] | None:
    """``(typical_seconds, upper_seconds)`` from at least one recorded start, else None."""
    loaded = _load(state_path())
    if loaded is None or loaded[1] < 1:
        return None
    current = kalman.predict(loaded[0], time.time() if now is None else now, DRIFT_PER_DAY)
    # A single start's spread is the typical's own uncertainty plus a start's scatter.
    spread = math.sqrt(current.variance + READING_VARIANCE)
    return math.exp(current.mean), math.exp(current.mean + UPPER_K * spread)


def retry_hint(elapsed_seconds: float | None, now: float | None = None) -> str | None:
    """The sentence the ``warming_up`` hint adds, or None without a recorded start."""
    estimate = typical(now)
    if estimate is None:
        return None
    usual, upper = estimate
    sentence = f"Starts here usually take about {usual:.0f} s, rarely more than {upper:.0f} s"
    if elapsed_seconds is not None and elapsed_seconds >= 0:
        remaining = max(1.0, usual - elapsed_seconds)
        if elapsed_seconds >= upper:
            return f"{sentence}; this one has run {elapsed_seconds:.0f} s, longer than usual."
        return f"{sentence}; this one has run {elapsed_seconds:.0f} s, so retry in about {remaining:.0f} s."
    return f"{sentence}."
