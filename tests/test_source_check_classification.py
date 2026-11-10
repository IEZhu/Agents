"""Pin how the source check reads a body that arrived under a status of 200.

The script itself needs a network and is deliberately not a test. This covers
the one decision in it that does not: which verdict a body earns. Review found
that decision ordered the wrong way round, so it is held here rather than in
someone's memory.
"""
from scripts.check_skill_sources import (
    MISSING_200,
    NOTFOUND_BODY,
    OK,
    REFUSAL_BODY,
    REFUSED_200,
    STUB,
    STUB_BYTES,
    classify,
)

# The newline matters. Without it the filler runs straight into the last word,
# and `\bnot found\b` stops matching — which made the both-signals case below
# vacuous on its first draft, and a mutant that flipped the precedence passed.
FILLER = b"\n" + b"x" * (STUB_BYTES + 100)


def test_a_refusal_page_is_a_refusal():
    assert classify(b"Access Denied" + FILLER) == REFUSED_200


def test_a_soft_404_is_a_wrong_url():
    assert classify(b"Error 404" + FILLER) == MISSING_200


def test_a_body_matching_both_is_a_refusal_not_a_wrong_url():
    """The asymmetry is the reason: only one of the two verdicts fails the run.

    A refusal page carries "not found" in its boilerplate often enough that the
    looser pattern would win and fail the run for a URL that is perfectly good.
    """
    both = b"Access Denied - the page you requested was not found" + FILLER
    assert NOTFOUND_BODY.search(both.decode()), "the case proves nothing unless both match"
    assert REFUSAL_BODY.search(both.decode()), "the case proves nothing unless both match"
    assert classify(both) == REFUSED_200


def test_a_short_body_is_a_stub():
    assert classify(b"<html></html>") == STUB


def test_an_ordinary_document_is_served():
    assert classify(b"Article 14.2 provides that the employer shall" + FILLER) == OK


def test_a_single_article_payload_is_not_a_stub():
    """About a kilobyte is the whole document for the BOE article API."""
    assert len(FILLER) > STUB_BYTES
    assert classify(b"<documento><texto>" + FILLER + b"</texto></documento>") == OK
