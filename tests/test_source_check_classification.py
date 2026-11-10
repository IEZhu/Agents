"""Pin how the source check reads a body that arrived under a status of 200.

The script itself needs a network and is deliberately not a test. This covers
the one decision in it that does not: which verdict a body earns. Review found
that decision ordered the wrong way round, so it is held here rather than in
someone's memory.
"""
import gzip
import zlib

from scripts.check_skill_sources import (
    MISSING_200,
    NOTFOUND_BODY,
    OK,
    REFUSAL_BODY,
    REFUSED_200,
    STUB,
    STUB_BYTES,
    classify,
    inflate,
)

# The newline matters. Without it the filler runs straight into the last word,
# and `\bnot found\b` stops matching — which made the both-signals case below
# vacuous on its first draft, and a mutant that flipped the precedence passed.
#
# FILLER is deliberately threshold-relative: the cases that use it only need a
# body over the floor, whatever the floor is. The one case that is *about* the
# floor uses ARTICLE instead, a fixed payload, so raising STUB_BYTES past the
# size of a real article breaks the test. Review caught that: with a relative
# payload that case could not fail, which is the whole defect it exists to
# catch.
FILLER = b"\n" + b"x" * (STUB_BYTES + 100)

# A single-article payload from the BOE consolidated-legislation API, the real
# shape that an earlier 2,000-byte floor wrongly called a stub. Fixed size on
# purpose; do not rewrite it in terms of STUB_BYTES. Named SINGLE_ARTICLE and not
# ARTICLE because ARTICLE is already the compression fixture further down -- on
# the first draft the later binding shadowed this one and the case below silently
# measured the wrong bytes.
SINGLE_ARTICLE = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n<response><data><bloque id="a56">'
    b"<titulo>Art\xc3\xadculo 56. Despido improcedente.</titulo><p>1. Cuando el "
    b"despido sea declarado improcedente, el empresario, en el plazo de cinco "
    b"d\xc3\xadas desde la notificaci\xc3\xb3n de la sentencia, podr\xc3\xa1 optar entre la "
    b"readmisi\xc3\xb3n del trabajador o el abono de una indemnizaci\xc3\xb3n equivalente "
    b"a treinta y tres d\xc3\xadas de salario por a\xc3\xb1o de servicio, "
    b"prorrate\xc3\xa1ndose por meses los periodos de tiempo inferiores a un a\xc3\xb1o, "
    b"hasta un m\xc3\xa1ximo de veinticuatro mensualidades. La opci\xc3\xb3n por la "
    b"indemnizaci\xc3\xb3n determinar\xc3\xa1 la extinci\xc3\xb3n del contrato de trabajo, que se "
    b"entender\xc3\xa1 producida en la fecha del cese efectivo en el trabajo.</p>"
    b"</bloque></data></response>"
)


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
    """A single article is the whole document, and it is small.

    This one is 715 bytes -- under a kilobyte, which is why the floor sits at
    500 and why the earlier 2,000 called two valid BOE articles stubs. The
    bounds below are the point of the case: raise STUB_BYTES past a real
    article and this fails. The first draft padded to STUB_BYTES + 100 instead,
    so it could not fail at any floor, which is the defect it exists to catch.
    """
    assert 600 < len(SINGLE_ARTICLE) < 1400, "keep this the size of a real single-article payload"
    assert classify(SINGLE_ARTICLE) == OK


# The eCFR versioner API answers 406 "This endpoint requires response
# compression" to a client that offers no Accept-Encoding. The check used to
# send none, so five live sources were filed as refusals and the pull request
# blamed the publisher. Asking for compression means being able to read it.

ARTICLE = b"Limitation on amount of securities sold. " * 40


def test_a_gzip_body_is_inflated():
    assert inflate(gzip.compress(ARTICLE), "gzip") == ARTICLE


def test_a_deflate_body_is_inflated():
    assert inflate(zlib.compress(ARTICLE), "deflate") == ARTICLE


def test_raw_deflate_without_the_zlib_wrapper_is_inflated():
    """Some servers send raw deflate; the fallback in inflate() is for them."""
    c = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    raw = c.compress(ARTICLE) + c.flush()
    assert inflate(raw, "deflate") == ARTICLE


def test_an_uncompressed_body_passes_through():
    """A server may ignore the request, and then nothing must be undone."""
    assert inflate(ARTICLE, "") == ARTICLE
    assert inflate(ARTICLE, "identity") == ARTICLE


def test_the_header_is_read_case_and_space_insensitively():
    assert inflate(gzip.compress(ARTICLE), " GZIP ") == ARTICLE


def test_a_compressed_document_is_judged_after_inflation_not_before():
    """Compressed, a real document can fall under the stub floor.

    This is why probe() inflates before classify() sees the body: the Rule 144
    payload compresses well, and judging the compressed bytes would call a
    served statute a stub.
    """
    compressed = gzip.compress(ARTICLE)
    assert len(compressed) < STUB_BYTES, "pick a body that actually compresses under the floor"
    assert classify(compressed) == STUB
    assert classify(inflate(compressed, "gzip")) == OK
