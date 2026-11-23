"""The analysis of a set's attempt records, and the coverage simulation that precedes a run.

docs/verifier-accuracy-measurement.md, "Analysis". Per configuration and family, model.py's
own model: an item's family reading is right with probability ``c`` and each verdict agrees
with the reading with probability ``b``, so an item with ``n`` verdicts of which ``r`` are
correct has likelihood ``c·b^r·(1−b)^(n−r) + (1−c)·(1−b)^r·b^(n−r)``. The posterior of
``(c, b)`` is computed on a grid with uniform priors over (1/2, 1) for ``c`` and (0, 1) for
``b``, the log-likelihood divided by the design effect ``D``. ``D`` is the variance of the
point estimates under cluster resampling over their variance under item resampling, never
below 1, and the mean cluster size when either variance is zero; the coverage simulation
raises a floor under it until every band holds its level. Bands are marginal quantiles:
0.00625/0.99375 at the final look (Bonferroni over c_model, b_V3, c_execution, b_V2) and
0.0005/0.9995 at the interim look. The symmetry, independence and per-stratum checks use
plain 95 % bands and produce findings for the owner, never values. Nothing here writes to
`src/claims/model.py` or the claim store.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import math
import statistics

import numpy as np

from src.claims import model as claims_model

QUANTILES = {"final": (0.00625, 0.99375), "interim": (0.0005, 0.9995)}
PLAIN = (0.025, 0.975)
NARROW = 0.03
FAMILY_LEVEL = {"model": "V3", "execution": "V2"}
PRIORS = {"model": {"c": claims_model.FAMILIES["model"], "b": claims_model.ROUTES["V3"].accuracy},
          "execution": {"c": claims_model.FAMILIES["execution"], "b": claims_model.ROUTES["V2"].accuracy}}
CATEGORIES = ((1, 0), (1, 1), (2, 0), (2, 1), (2, 2))  # (verdicts, correct verdicts) of one item
CONFIG_KEYS = ("client", "tier", "model", "reasoning_effort", "bundle_revision")
SCENARIO_B = 0.995


class Grid:
    """Midpoint cells over c in (1/2, 1) and b in (0, 1), with each category's log-likelihood."""

    def __init__(self, c_cells: int = 500, b_cells: int = 1000):
        self.c_edges = np.linspace(0.5, 1.0, c_cells + 1)
        self.b_edges = np.linspace(0.0, 1.0, b_cells + 1)
        self.c = (self.c_edges[:-1] + self.c_edges[1:]) / 2
        self.b = (self.b_edges[:-1] + self.b_edges[1:]) / 2
        c, b = self.c[:, None], self.b[None, :]
        self.terms = np.stack([
            np.log(c * b ** r * (1 - b) ** (n - r) + (1 - c) * (1 - b) ** r * b ** (n - r)).ravel()
            for n, r in CATEGORIES])

    def mle(self, counts: np.ndarray) -> tuple[float, float]:
        return tuple(self.mle_many(counts[None, :])[0])

    def mle_many(self, counts: np.ndarray) -> np.ndarray:
        index = np.argmax(counts @ self.terms, axis=1)
        return np.stack([self.c[index // len(self.b)], self.b[index % len(self.b)]], axis=1)

    def marginals(self, counts: np.ndarray, design_effect: float) -> tuple[np.ndarray, np.ndarray]:
        log = (counts @ self.terms) / design_effect
        weights = np.exp(log - log.max()).reshape(len(self.c), len(self.b))
        weights /= weights.sum()
        return weights.sum(axis=1), weights.sum(axis=0)


def quantile(edges: np.ndarray, mass: np.ndarray, q: float) -> float:
    """The ``q`` quantile of a piecewise-uniform density on ``edges``."""
    cdf = np.concatenate([[0.0], np.cumsum(mass)])
    k = int(np.clip(np.searchsorted(cdf, q, side="left"), 1, len(edges) - 1))
    low, high = cdf[k - 1], cdf[k]
    fraction = 0.0 if high <= low else (q - low) / (high - low)
    return float(edges[k - 1] + fraction * (edges[k] - edges[k - 1]))


@dataclass(frozen=True)
class Unit:
    """One item's verdicts of one family's level."""

    item: str
    cluster: str
    truth: bool
    n: int
    r: int

    @property
    def category(self) -> int:
        return CATEGORIES.index((self.n, self.r))


def counts(units: list[Unit]) -> np.ndarray:
    vector = np.zeros(len(CATEGORIES))
    for unit in units:
        vector[unit.category] += 1
    return vector


def design_effect(units: list[Unit], coarse: Grid, rng: np.random.Generator, resamples: int) -> dict:
    one_hot = np.eye(len(CATEGORIES))[[unit.category for unit in units]]
    clusters: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(len(CATEGORIES)))
    for unit, row in zip(units, one_hot):
        clusters[unit.cluster] = clusters[unit.cluster] + row
    by_cluster = np.stack(list(clusters.values()))
    item_draws = rng.integers(0, len(units), size=(resamples, len(units)))
    cluster_draws = rng.integers(0, len(by_cluster), size=(resamples, len(by_cluster)))
    by_items = coarse.mle_many(one_hot[item_draws].sum(axis=1))
    by_clusters = coarse.mle_many(by_cluster[cluster_draws].sum(axis=1))
    mean_size = len(units) / len(by_cluster)
    ratios = {}
    for column, name in enumerate(("c", "b")):
        # Identical estimates have zero variance; np.var of equal floats can leave a rounding
        # residue, so zero is decided by the spread, not by the variance.
        if np.ptp(by_items[:, column]) == 0 or np.ptp(by_clusters[:, column]) == 0:
            ratios[name] = mean_size
        else:
            ratios[name] = float(np.var(by_clusters[:, column], ddof=1) / np.var(by_items[:, column], ddof=1))
    return {"D": max(1.0, *ratios.values()), "ratios": ratios, "mean_cluster_size": mean_size,
            "clusters": len(by_cluster)}


def _rule(look: str, prior: float, estimate: float, low: float, high: float) -> dict:
    """Which of the replacement rule's four cases the band falls in; a finding, nothing applied."""
    narrow = (high - low) / 2 <= NARROW
    if look == "interim":
        return {"rule": 3, "proposed": high} if high < prior else {"rule": None, "proposed": None}
    if narrow and estimate <= prior:
        return {"rule": 1, "proposed": estimate}
    if narrow:
        return {"rule": 2, "proposed": None, "after_audit": max(prior, low)}
    if high < prior:
        return {"rule": 3, "proposed": high}
    return {"rule": 4, "proposed": None}


def fit(units: list[Unit], *, family: str, look: str, grid: Grid, coarse: Grid, rng: np.random.Generator,
        resamples: int, d_floor: float = 1.0) -> dict:
    if not units:
        return {"items": 0}
    vector = counts(units)
    effect = design_effect(units, coarse, rng, resamples)
    used = max(effect["D"], d_floor)
    estimate = dict(zip(("c", "b"), grid.mle(vector)))
    marginal = dict(zip(("c", "b"), grid.marginals(vector, used)))
    edges = {"c": grid.c_edges, "b": grid.b_edges}
    low_q, high_q = QUANTILES[look]
    parameters = {}
    for name in ("c", "b"):
        low, high = quantile(edges[name], marginal[name], low_q), quantile(edges[name], marginal[name], high_q)
        prior = PRIORS[family][name]
        parameters[name] = {"estimate": round(estimate[name], 4),
                            "median": round(quantile(edges[name], marginal[name], 0.5), 4),
                            "band": [round(low, 4), round(high, 4)], "half_width": round((high - low) / 2, 4),
                            "prior": prior, **_rule(look, prior, estimate[name], low, high)}
    below_chance = parameters["b"]["band"][1] < 0.5
    if below_chance:  # a finding, never a value: more verdicts of such a route make things worse
        parameters["b"] = {key: value for key, value in parameters["b"].items() if key != "after_audit"}
        parameters["b"].update(rule=None, proposed=None)
    return {"items": len(units), "verdicts": int(sum(unit.n for unit in units)),
            "categories": {f"{n}/{r}": int(vector[index]) for index, (n, r) in enumerate(CATEGORIES)},
            "design_effect": {**effect, "used": used, "floor": d_floor},
            "parameters": parameters, "below_chance": below_chance}


def configuration(row: dict) -> tuple:
    return tuple(row.get(key) for key in CONFIG_KEYS)


def usable(rows: list[dict]) -> list[dict]:
    """Verdicts that may be fitted: not discarded for blinding, not excluded for a missing model."""
    return [row for row in rows if row["status"] == "verdict" and not row.get("discarded")
            and not row.get("excluded") and row.get("model")]


def correct(row: dict) -> bool:
    return row["outcome"] in ("TP", "TN")


def family_units(rows: list[dict], family: str, clusters: dict[str, str]) -> tuple[dict, int]:
    """Units per configuration, and how many items were left out for mixing configurations."""
    level = FAMILY_LEVEL[family]
    per_item: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row.get("level") == level:
            per_item[row["item"]].append(row)
    by_configuration: dict[tuple, list[Unit]] = defaultdict(list)
    mixed = 0
    for item, verdicts in sorted(per_item.items()):
        configurations = {configuration(row) for row in verdicts}
        if len(configurations) != 1:
            mixed += 1
            continue
        verdicts = verdicts[:2]
        by_configuration[configurations.pop()].append(
            Unit(item, clusters[item], verdicts[0]["truth"], len(verdicts), sum(map(correct, verdicts))))
    return dict(by_configuration), mixed


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _rates(rows: list[dict]) -> dict:
    tally = Counter(row["outcome"] for row in rows)
    return {"accuracy": _ratio(tally["TP"] + tally["TN"], len(rows)),
            "sensitivity": _ratio(tally["TP"], tally["TP"] + tally["FN"]),
            "specificity": _ratio(tally["TN"], tally["TN"] + tally["FP"]), "counts": dict(tally)}


def _percentile_band(values: list[float]) -> list[float] | None:
    values = [value for value in values if value is not None]
    if len(values) < 20:
        return None
    return [round(float(np.quantile(values, PLAIN[0])), 4), round(float(np.quantile(values, PLAIN[1])), 4)]


def _cluster_resamples(rows: list[dict], clusters: dict[str, str], rng, resamples: int):
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[clusters[row["item"]]].append(row)
    names = sorted(groups)
    for _ in range(resamples):
        drawn = rng.integers(0, len(names), size=len(names))
        # A cluster drawn twice contributes two separate copies: its items get distinct IDs, so
        # per-item statistics do not merge the copies into one item with doubled verdicts.
        yield [{**row, "item": f"{row['item']}#{copy}"} for copy, index in enumerate(drawn)
               for row in groups[names[index]]]


def verdict_rates(rows: list[dict], clusters: dict[str, str], rng, resamples: int) -> dict:
    """Single-verdict accuracy, sensitivity and specificity with plain cluster-bootstrap bands."""
    if not rows:
        return {"verdicts": 0}
    rates = _rates(rows)
    samples = defaultdict(list)
    for sample in _cluster_resamples(rows, clusters, rng, resamples):
        drawn = _rates(sample)
        for key in ("accuracy", "sensitivity", "specificity"):
            samples[key].append(drawn[key])
        if drawn["sensitivity"] is not None and drawn["specificity"] is not None:
            samples["difference"].append(drawn["specificity"] - drawn["sensitivity"])
    bands = {key: _percentile_band(values) for key, values in samples.items()}
    difference = (rates["specificity"] - rates["sensitivity"]
                  if rates["specificity"] is not None and rates["sensitivity"] is not None else None)
    asymmetric = bool(bands.get("difference") and (bands["difference"][0] > 0 or bands["difference"][1] < 0))
    return {"verdicts": len(rows), **{key: None if value is None else round(value, 4)
                                      for key, value in rates.items() if key != "counts"},
            "counts": rates["counts"], "bands": bands,
            "specificity_minus_sensitivity": None if difference is None else round(difference, 4),
            "asymmetric": asymmetric}


def _independence_once(rows: list[dict]) -> tuple[float, float, int] | None:
    per_item: dict[str, dict] = defaultdict(lambda: {"V3": [], "V2": []})
    for row in rows:
        if row.get("level") in ("V3", "V2"):
            per_item[row["item"]][row["level"]].append(row)
    paired = [(verdicts, verdicts["V3"][0]["truth"]) for verdicts in per_item.values()
              if verdicts["V3"] and verdicts["V2"]]
    if not paired:
        return None
    wrong = {truth: {level: [0, 0] for level in ("V3", "V2")} for truth in (True, False)}
    for verdicts, truth in paired:
        for level in ("V3", "V2"):
            wrong[truth][level][0] += sum(not correct(row) for row in verdicts[level])
            wrong[truth][level][1] += len(verdicts[level])
    observed = expected = pairs = 0
    for verdicts, truth in paired:
        rate3 = wrong[truth]["V3"][0] / wrong[truth]["V3"][1]
        rate2 = wrong[truth]["V2"][0] / wrong[truth]["V2"][1]
        n3, n2 = len(verdicts["V3"]), len(verdicts["V2"])
        observed += sum(not correct(row) for row in verdicts["V3"]) * sum(not correct(row) for row in verdicts["V2"])
        expected += n3 * n2 * rate3 * rate2
        pairs += n3 * n2
    return observed, expected, pairs


def independence(rows: list[dict], clusters: dict[str, str], rng, resamples: int) -> dict:
    """Coincident V3 and V2 failures on the same items against the independence prediction."""
    once = _independence_once(rows)
    if once is None:
        return {"items": 0}
    observed, expected, pairs = once
    excess = []
    for sample in _cluster_resamples(rows, clusters, rng, resamples):
        drawn = _independence_once(sample)
        if drawn is not None and drawn[2]:
            excess.append((drawn[0] - drawn[1]) / drawn[2])
    band = _percentile_band(excess)
    return {"pairs": pairs, "observed": observed, "expected": round(expected, 3),
            "excess_rate": round((observed - expected) / pairs, 4), "band": band,
            "dependent": bool(band and band[0] > 0)}


def wilson(successes: int, total: int, z: float = 1.959964) -> list[float] | None:
    if not total:
        return None
    p = successes / total
    centre = (p + z * z / (2 * total)) / (1 + z * z / total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / (1 + z * z / total)
    return [round(centre - spread, 4), round(centre + spread, 4)]


def strata_rates(rows: list[dict]) -> dict:
    """Per-level, per-stratum single-verdict accuracy (Wilson, ignoring clusters) and transport findings."""
    out = {}
    for level in ("V3", "V2"):
        level_rows = [row for row in rows if row.get("level") == level]
        if not level_rows:
            continue
        pooled = wilson(sum(map(correct, level_rows)), len(level_rows))
        per = {}
        for stratum in sorted({row["stratum"] for row in level_rows}):
            members = [row for row in level_rows if row["stratum"] == stratum]
            band = wilson(sum(map(correct, members)), len(members))
            per[stratum] = {"verdicts": len(members), "accuracy": round(sum(map(correct, members)) / len(members), 4),
                            "band": band, "below_pooled": bool(band and pooled and band[1] < pooled[0])}
        out[level] = {"pooled": pooled, "strata": per}
    return out


def effective(rows: list[dict]) -> list[dict]:
    """One row per scheduled slot: its latest. A resumed run reschedules only a slot whose rows are
    placeholders (it never ran), so the latest row is the attempt that ran, or the last stop."""
    latest: dict[tuple, dict] = {}
    for row in rows:
        latest[(row["item"], row["arm"], row["slot"])] = row
    return list(latest.values())


def tally(rows: list[dict]) -> dict:
    """Statuses per arm and class, the levels assigned, and what was set aside and why; each slot once."""
    superseded = len(rows) - len(rows := effective(rows))
    statuses = {arm: {name: Counter() for name in ("true", "false")} for arm in ("V3", "V2")}
    levels = {arm: Counter() for arm in ("V3", "V2")}
    reasons, set_aside = Counter(), Counter()
    for row in rows:
        statuses[row["arm"]]["true" if row["truth"] else "false"][row["status"]] += 1
        if row["status"] == "verdict":
            levels[row["arm"]][row["level"]] += 1
        if row.get("reason"):
            reasons[f"{row['status']}:{row['reason']}"] += 1
        for key in ("discarded", "excluded"):
            if row.get(key):
                set_aside[f"{key}:{row[key]}"] += 1
    return {"attempts": len(rows), "superseded_placeholders": superseded,
            "statuses": {arm: {name: dict(counter) for name, counter in by_class.items()}
                         for arm, by_class in statuses.items()},
            "levels": {arm: dict(counter) for arm, counter in levels.items()},
            "reasons": dict(reasons), "set_aside": dict(set_aside)}


def cost(rows: list[dict], probes: list[dict]) -> dict:
    """Host-reported cost per worker run (API list-price equivalent), by arm and in total."""
    def summary(values: list[float]) -> dict:
        if not values:
            return {"runs": 0}
        return {"runs": len(values), "total_usd": round(sum(values), 4), "mean_usd": round(statistics.fmean(values), 4),
                "median_usd": round(statistics.median(values), 4), "max_usd": round(max(values), 4)}
    by_arm = {arm: summary([row["cost_usd"] for row in rows if row["arm"] == arm and row.get("cost_usd") is not None])
              for arm in ("V3", "V2")}
    durations = [row["duration_ms"] / 1000 for row in rows if row.get("duration_ms") is not None]
    turns = [row["num_turns"] for row in rows if row.get("num_turns") is not None]
    runs = [row["cost_usd"] for row in rows if row.get("cost_usd") is not None]
    probe_costs = [probe["cost_usd"] for probe in probes if probe.get("cost_usd") is not None]
    return {"by_arm": by_arm, "all_runs": summary(runs), "probes": summary(probe_costs),
            "mean_seconds": round(statistics.fmean(durations), 1) if durations else None,
            "mean_turns": round(statistics.fmean(turns), 1) if turns else None}


def analyze(rows: list[dict], statements: dict[str, dict], *, look: str = "final", d_floor: float = 1.0,
            seed: int = 0, resamples: int = 400, probes: list[dict] | None = None,
            grid: Grid | None = None, coarse: Grid | None = None) -> dict:
    """The whole report, aggregates only: no statement, label or per-item verdict."""
    rng = np.random.default_rng(seed)
    grid, coarse = grid or Grid(), coarse or Grid(100, 200)
    clusters = {item: row["cluster"] for item, row in statements.items()}
    fitted = usable(effective(rows))
    configurations = []
    mixed = {}
    findings = []
    by_configuration: dict[tuple, dict] = defaultdict(dict)
    for family in FAMILY_LEVEL:
        units, mixed[family] = family_units(fitted, family, clusters)
        for key, members in units.items():
            by_configuration[key][family] = {
                subset: fit([unit for unit in members if subset == "all" or unit.truth == (subset == "true")],
                            family=family, look=look, grid=grid, coarse=coarse, rng=rng, resamples=resamples,
                            d_floor=d_floor)
                for subset in ("all", "true", "false")}
    for key in sorted(by_configuration, key=lambda key: tuple(str(part) for part in key)):
        rows_here = [row for row in fitted if configuration(row) == key]
        levels = {level: verdict_rates([row for row in rows_here if row["level"] == level], clusters, rng, resamples)
                  for level in ("V3", "V2")}
        entry = {"configuration": dict(zip(CONFIG_KEYS, key)), "families": by_configuration[key], "levels": levels,
                 "independence": independence(rows_here, clusters, rng, resamples),
                 "strata": strata_rates(rows_here)}  # within one configuration: a mix of them is no stratum effect
        configurations.append(entry)
        for family, subsets in entry["families"].items():
            overall = subsets["all"]
            if overall.get("below_chance"):
                findings.append(f"{family}: the band for b lies wholly below 1/2 (below chance)")
            for name, parameter in overall.get("parameters", {}).items():
                if parameter.get("rule") in (1, 2, 3):
                    findings.append(f"{family} {name}: replacement rule {parameter['rule']} applies (reported, not applied)")
        for level, rates in levels.items():
            if rates.get("asymmetric"):
                findings.append(f"{level}: specificity minus sensitivity excludes zero (symmetry check)")
        if entry["independence"].get("dependent"):
            findings.append("V3 and V2 fail together more often than independence predicts")
        model_name = entry["configuration"]["model"]
        for level, section in entry["strata"].items():
            for stratum, values in section["strata"].items():
                if values["below_pooled"]:
                    findings.append(f"{model_name} {level} {stratum}: band wholly below the pooled one (transport)")
    return {"look": look, "d_floor": d_floor, "tally": tally(rows), "mixed_configuration_items": mixed,
            "configurations": configurations, "cost": cost(rows, probes or []), "findings": findings}


# --- Coverage simulation ----------------------------------------------------------------------


def _simulated_units(c: float, b: float, cluster_sizes: list[int], rng: np.random.Generator,
                     verdicts: int = 2) -> list[Unit]:
    units = []
    for index, size in enumerate(cluster_sizes):
        right = rng.random(size) < c
        correct_probability = np.where(right, b, 1 - b)
        hits = (rng.random((size, verdicts)) < correct_probability[:, None]).sum(axis=1)
        units += [Unit(f"s{index}-{j}", f"k{index}", True, verdicts, int(hit)) for j, hit in enumerate(hits)]
    return units


def coverage(*, family: str, c: float, b: float, cluster_sizes: list[int], sets: int, resamples: int, look: str,
             d_floor: float, rng: np.random.Generator, grid: Grid, coarse: Grid) -> dict:
    low_q, high_q = QUANTILES[look]
    covered = Counter()
    for _ in range(sets):
        result = fit(_simulated_units(c, b, cluster_sizes, rng), family=family, look=look, grid=grid,
                     coarse=coarse, rng=rng, resamples=resamples, d_floor=d_floor)
        for name, truth in (("c", c), ("b", b)):
            low, high = result["parameters"][name]["band"]
            covered[name] += low <= truth <= high
    nominal = high_q - low_q
    rates = {name: covered[name] / sets for name in ("c", "b")}
    holds = {name: wilson(covered[name], sets)[1] >= nominal for name in ("c", "b")}
    return {"family": family, "c": c, "b": b, "items": sum(cluster_sizes), "sets": sets, "nominal": nominal,
            "coverage": rates, "holds": holds}


def scenarios(model_sizes: list[int], execution_sizes: list[int]) -> list[dict]:
    """The priors, and each family at b = 0.995, as the note asks."""
    out = []
    for family, sizes in (("model", model_sizes), ("execution", execution_sizes)):
        if sizes:
            prior = PRIORS[family]
            out.append({"family": family, "c": prior["c"], "b": prior["b"], "cluster_sizes": sizes})
            out.append({"family": family, "c": prior["c"], "b": SCENARIO_B, "cluster_sizes": sizes})
    return out


def calibrate(planned: list[dict], *, sets: int = 200, resamples: int = 200, look: str = "final", seed: int = 0,
              step: float = 1.25, ceiling: float = 16.0, grid: Grid | None = None, coarse: Grid | None = None) -> dict:
    """The smallest floor under D at which every simulated band holds its level."""
    grid, coarse = grid or Grid(), coarse or Grid(100, 200)
    floor = 1.0
    while True:
        rng = np.random.default_rng(seed)
        results = [coverage(family=scenario["family"], c=scenario["c"], b=scenario["b"],
                            cluster_sizes=scenario["cluster_sizes"], sets=sets, resamples=resamples, look=look,
                            d_floor=floor, rng=rng, grid=grid, coarse=coarse) for scenario in planned]
        if all(all(result["holds"].values()) for result in results):
            return {"d_floor": round(floor, 4), "look": look, "held": True, "scenarios": results}
        if floor * step > ceiling:
            return {"d_floor": round(floor, 4), "look": look, "held": False, "scenarios": results}
        floor *= step
