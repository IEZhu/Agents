"""Shared external allowance keys and wait extraction; no platform calls."""
import pytest

from src.cooperation import quota
from src.cooperation.models import Checkpoint, ExternalQuotaObservation

# Deliberately not a reviewer this source knows anything about.
UNKNOWN = "Nebula-Reviewer[bot]"


@pytest.mark.parametrize("host,account,reviewer", [
    ("github.com", "IEZhu", "coderabbitai[bot]"),
    ("https://GitHub.com/IEZhu/Agents", "iezhu", "CodeRabbitAI"),
    ("git@github.com:1234", "IEzhu", " coderabbitai[bot] "),
])
def test_one_allowance_survives_every_spelling_of_its_platform_surfaces(host, account, reviewer):
    assert quota.quota_key(host, account, reviewer) == "github.com/iezhu:coderabbitai"
    assert quota.parse_quota_key("github.com/iezhu:coderabbitai") == ("github.com", "iezhu", "coderabbitai")


def test_the_key_names_the_account_and_the_reviewer_but_never_the_repository():
    key = quota.quota_key("github.com", "iezhu", "coderabbitai")
    assert key == "github.com/iezhu:coderabbitai"
    other_account = quota.quota_key("github.com", "wondermr", "coderabbitai")
    other_reviewer = quota.quota_key("github.com", "iezhu", UNKNOWN)
    assert len({key, other_account, other_reviewer}) == 3
    assert "agents" not in quota.quota_key("github.com", "iezhu", "coderabbitai")


@pytest.mark.parametrize("key", ["github.com", "github.com/iezhu", "iezhu:coderabbitai",
                                 "github.com/iezhu:code rabbit", "github.com//:coderabbitai",
                                 "github.com/iezhu:", "  ", "github.com/../dev:coderabbitai",
                                 "https://github.com/iezhu:coderabbitai", "github.com/iezhu/agents:coderabbitai",
                                 "github.com:1234/iezhu:coderabbitai", "user@github.com/iezhu:coderabbitai"])
def test_a_key_that_is_not_an_allowance_triple_is_refused(key):
    with pytest.raises(ValueError):
        quota.canonical_quota_key(key)


def test_an_unknown_reviewer_is_still_paused_on_the_default_backoff():
    evidence = "Nebula: your review budget for this account is spent. Check back later."
    pause = quota.observed_pause(UNKNOWN, evidence, observed_at=2000.0)
    assert pause.reviewer == "nebula-reviewer"
    assert (pause.source, pause.rule_id) == ("default", None)
    assert pause.wait_seconds == quota.DEFAULT_BACKOFF_SECONDS
    assert pause.next_free_at == 2000.0 + quota.DEFAULT_BACKOFF_SECONDS
    assert pause.quote == evidence


def test_an_unknown_reviewer_that_states_its_wait_is_believed():
    pause = quota.observed_pause(UNKNOWN, "Nebula hit a limit; try again in 7 minutes.", observed_at=2000.0)
    assert (pause.source, pause.rule_id) == ("stated", "stated_minutes")
    assert pause.wait_seconds == 420.0
    assert pause.next_free_at == 2420.0
    assert pause.quote == "try again in 7 minutes"


@pytest.mark.parametrize("reviewer,evidence,rule,seconds,source", [
    ("coderabbitai[bot]", "Review limit reached. Next included review available in 23 minutes.",
     "coderabbit_included_review", 1380.0, "stated"),
    ("copilot-pull-request-reviewer[bot]", "Copilot: the user who requested the review has reached their quota limit.",
     "copilot_account_quota", 86400.0, "rule"),
    ("Copilot", "...has reached their quota limit.", "copilot_account_quota", 86400.0, "rule"),
    (UNKNOWN, "Rate limited: retry after 90 seconds.", "stated_seconds", 90.0, "stated"),
    (UNKNOWN, "Out of budget, available in 2 hours.", "stated_hours", 7200.0, "stated"),
])
def test_the_matcher_is_data_and_the_wait_comes_from_the_evidence(reviewer, evidence, rule, seconds, source):
    pause = quota.observed_pause(reviewer, evidence, observed_at=100.0)
    assert (pause.rule_id, pause.wait_seconds, pause.source) == (rule, seconds, source)
    assert pause.quote in evidence


def test_the_wait_is_measured_from_the_evidence_not_from_reading_it_later():
    early = quota.observed_pause("coderabbitai", "Next included review available in 10 minutes.", observed_at=1000.0)
    late = quota.observed_pause("coderabbitai", "Next included review available in 10 minutes.", observed_at=1500.0)
    assert (early.next_free_at, late.next_free_at) == (1600.0, 2100.0)


@pytest.mark.parametrize("observed_at,default_seconds", [(-1.0, 3600.0), (True, 3600.0), (0.0, 0.0), (0.0, -60.0)])
def test_a_pause_needs_a_real_time_and_a_positive_default(observed_at, default_seconds):
    with pytest.raises(ValueError):
        quota.observed_pause(UNKNOWN, "limit reached", observed_at=observed_at, default_seconds=default_seconds)


def test_an_absurd_stated_wait_is_capped_while_its_quote_is_kept():
    pause = quota.observed_pause(UNKNOWN, "Limit reached, retry after 999999999 minutes.", observed_at=0.0)
    assert pause.wait_seconds == quota.MAX_WAIT_SECONDS
    assert pause.next_free_at == quota.MAX_WAIT_SECONDS
    assert pause.quote == "retry after 999999999 minutes"
    with pytest.raises(ValueError):
        quota.observed_pause(UNKNOWN, "limit", observed_at=0.0,
                             default_seconds=quota.MAX_WAIT_SECONDS + 1)


def test_every_rule_either_extracts_a_wait_or_states_a_fixed_one():
    for rule in quota.WAIT_RULES:
        assert (rule.unit_seconds is None) != (rule.fixed_seconds is None), rule.rule_id
    assert len({rule.rule_id for rule in quota.WAIT_RULES}) == len(quota.WAIT_RULES)
    with pytest.raises(ValueError):
        quota.WaitRule("both", pattern="x", unit_seconds=60.0, fixed_seconds=60.0)
    with pytest.raises(ValueError):
        quota.WaitRule("neither", pattern="x")


def test_published_observations_fit_the_closed_checkpoint_contract():
    key = "GitHub.com/IEZhu:Nebula-Reviewer[bot]"
    pause = quota.observed_pause(UNKNOWN, "limit reached, retry after 30 seconds", observed_at=500.0)
    published = ExternalQuotaObservation.model_validate(quota.pause_observation(key, pause))
    assert published.resource_key == "github.com/iezhu:nebula-reviewer"
    assert (published.kind, published.next_free_at, published.wait_source) == ("paused", 530.0, "stated")
    consumed = ExternalQuotaObservation.model_validate(
        quota.consumed_observation(key, "reviewed the push without a request", observed_at=500.0))
    assert (consumed.kind, consumed.next_free_at, consumed.wait_source) == ("consumed", None, None)
    checkpoint = Checkpoint.model_validate({"kind": "checkpoint", "checkpoint_id": "round-1",
                                            "external_quota": [published.model_dump(), consumed.model_dump()]})
    assert [observation.kind for observation in checkpoint.external_quota] == ["paused", "consumed"]
    assert Checkpoint.model_validate({"kind": "checkpoint", "checkpoint_id": "none"}).external_quota == []


@pytest.mark.parametrize("invalid", [
    {"kind": "paused", "next_free_at": None, "wait_source": None},
    {"kind": "paused", "next_free_at": 600.0, "wait_source": None},
    {"kind": "consumed", "next_free_at": None, "wait_source": "stated"},
    {"kind": "paused", "next_free_at": 400.0, "wait_source": "stated"},
])
def test_a_published_pause_must_carry_the_next_free_time_it_claims(invalid):
    record = {"resource_key": "github.com/iezhu:coderabbitai", "evidence": "limit reached",
              "observed_at": 500.0, **invalid}
    with pytest.raises(ValueError):
        ExternalQuotaObservation.model_validate(record)


def test_the_claimable_resource_is_a_write_on_the_canonical_key():
    assert quota.resource("GitHub.com/IEZhu:CodeRabbitAI[bot]") == {
        "resource_type": "external_quota", "resource_key": "github.com/iezhu:coderabbitai", "access": "write"}


def test_a_platform_spelling_normalizes_while_a_key_carrying_one_is_refused():
    # Building a key from what the platform reports keeps the flexible host
    # handling; reading a key back must refuse what it does not recognise,
    # or two malformed spellings would alias onto the real allowance.
    assert quota.quota_key("git@github.com:1234", "iezhu", "coderabbitai") == "github.com/iezhu:coderabbitai"
    assert quota.quota_key("https://github.com/iezhu/agents", "iezhu", "coderabbitai") == "github.com/iezhu:coderabbitai"
    for aliased in ("github.com:1234/iezhu:coderabbitai", "user@github.com/iezhu:coderabbitai",
                    "https://github.com/iezhu:coderabbitai"):
        with pytest.raises(ValueError, match="bare hostname"):
            quota.canonical_quota_key(aliased)
    # The [bot] suffix still folds: that is one identity across two surfaces.
    assert quota.canonical_quota_key("github.com/iezhu:CodeRabbitAI[bot]") == "github.com/iezhu:coderabbitai"
    assert quota.bare_host(" GitHub.com ") == "github.com"
