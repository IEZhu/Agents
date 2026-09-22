"""Intent classification: decouple *how to reason* from *who answers*.

The enrichment budget used to come from :func:`enrichment.infer_tier`, a
three-line rule over query length plus one regex. Measured on the committed
golden set (``evals/datasets/routing.jsonl``, 110 labeled samples) that rule
scores 67/110 = 60.9%, and its errors are systematic: 16 of 25 ``standard``
samples are pushed to ``deep`` and 18 of 57 ``lite`` samples to ``standard``,
because ``len > 300`` alone forces the heaviest tier.

This module replaces that single length axis with two orthogonal ones:

``mode``
    What kind of task this is — the reasoning *method* the answer needs. It is
    independent of the persona: a lawyer and an engineer both "analyze".

``tier``
    The enrichment *budget*. Derived from the mode and a bounded structural
    score, never from length alone. Kept in the legacy ``lite``/``standard``/
    ``deep`` vocabulary so nothing on the wire, in the session cache key or in
    the eval labels changes shape.

Design constraints, in priority order:

1. **Pure and synchronous.** No embeddings, no I/O, no vector store, no network.
   ``classify_intent`` is called on the hot path before any ``await`` in
   ``server.route_and_load``; it must not add latency there, and it must be
   unit-testable without the live stores (there is no ``tests/conftest.py`` to
   isolate ``DATA_DIR``).
2. **No new dependencies.** Standard library only.
3. **Fail open.** Every knob reads through ``config._int_env``; the whole layer
   is gated by ``INTENT_CLASSIFIER_ENABLED`` and defaults to *off* so the
   legacy rule stays authoritative until an A/B says otherwise.

An embedding-centroid variant was considered and rejected for now: measured on
this checkout it moves ~3.6% of input tokens while adding 12–53 ms to a hot
path whose p95 is 37–58 ms, and it introduces a persisted centroid artifact
that can drift out of sync with ``EMBEDDING_MODEL`` exactly the way
``data/.skills_hash`` already does. The structural layer below carries the bulk
of the achievable accuracy with none of that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal, Optional, Sequence

from src.engine.config import (
    IMPLANTS_DEEP_TIER_DEFAULT,
    INTENT_CONVERSE_MAX_CHARS,
    INTENT_DEEP_AT,
    INTENT_LONG_CHARS,
    INTENT_VERY_LONG_CHARS,
)

Tier = Literal["lite", "standard", "deep"]

TaskMode = Literal[
    "converse",   # greeting, acknowledgement, small talk, capability question
    "retrieve",   # single fact or short lookup
    "create",     # produce prose or another artifact
    "explain",    # teach or describe something
    "operate",    # act on a system: code, config, command
    "analyze",    # multi-step reasoning, comparison, audit, investigation
    "compute",    # quantitative derivation, proof, calculation
]

SkillRender = Literal["compiled", "full"]

#: Ordered widest-to-narrowest so ``_MODE_POLICY`` stays readable; the tier here
#: is the mode's *default* budget, which the structural score may shift by one
#: step in either direction (see :func:`_resolve_tier`).
#:
#: ``skills`` mirrors today's ``_n_results_for_tier`` (lite 0 / standard 2 /
#: deep 4) so that a mode landing on its default tier requests exactly what the
#: legacy path would have requested. ``implants`` mirrors the legacy base count
#: (standard 2 / deep ``IMPLANTS_DEEP_TIER_DEFAULT``); the ``preferred_implants``
#: floor and the ``MAX_PREFERRED_IMPLANTS`` cap are applied by the caller, not
#: here, because they are per-agent facts this module deliberately does not know.
#:
#: ``suppress_format`` is deliberately limited to ``converse``. #64's table also
#: suppresses it for ``create`` and ``retrieve``, but a persona's Output Format
#: is not always a mere response template: for ``medical_expert`` it is where the
#: mandated ``### Safety`` section lives (red flags, contraindications), and
#: ``create`` fires on "summarize"/"draft"/"write", a large share of real
#: traffic. Widening this needs a per-section allowlist, not a per-mode flag.
#: Budget per TIER. Split out of ``_MODE_POLICY`` after review: the budget was
#: previously read with ``next(p for p in _MODE_POLICY.values() if p["tier"] == t)``,
#: which only ever reads the FIRST mode declaring a tier. That made the per-mode
#: ``skills``/``implants``/``render`` entries dead configuration — giving
#: ``operate`` its own pool size would have silently kept ``create``'s.
#:
#: The values reproduce the legacy derivation exactly: skills 0/2/4 from
#: ``_n_results_for_tier``, implants 2 at standard and IMPLANTS_DEEP_TIER_DEFAULT
#: at deep, and ``compiled`` only at standard (``use_compiled = tier == "standard"``).
#: The ``preferred_implants`` floor and the MAX_PREFERRED_IMPLANTS cap are applied
#: by the caller, not here: they are per-agent facts this module does not know.
_TIER_BUDGET: dict[Tier, dict] = {
    "lite":     {"skills": 0, "implants": 0, "render": "full"},
    "standard": {"skills": 2, "implants": 2, "render": "compiled"},
    "deep":     {"skills": 4, "implants": IMPLANTS_DEEP_TIER_DEFAULT, "render": "full"},
}

#: Per-MODE policy. Only fields the mode genuinely owns live here: the tier it
#: defaults to, and whether the persona's response template should be silenced.
#:
#: ``suppress_format`` is deliberately limited to ``converse``. #64's table also
#: suppresses it for ``create`` and ``retrieve``, but a persona's Output Format is
#: not always a mere response template: for ``medical_expert`` it is where the
#: mandated ``### Safety`` section lives, and ``create`` fires on
#: "summarize"/"draft"/"write", a large share of real traffic. Widening this needs
#: a per-section allowlist, not a per-mode flag.
_MODE_POLICY: dict[str, dict] = {
    "converse": {"tier": "lite",     "suppress_format": True},
    "retrieve": {"tier": "lite",     "suppress_format": False},
    "create":   {"tier": "standard", "suppress_format": False},
    "explain":  {"tier": "standard", "suppress_format": False},
    "operate":  {"tier": "standard", "suppress_format": False},
    "analyze":  {"tier": "deep",     "suppress_format": False},
    "compute":  {"tier": "deep",     "suppress_format": False},
}

_TIER_ORDER: tuple[Tier, ...] = ("lite", "standard", "deep")

#: Keys are whole query strings, so keep the memo small.
_CLASSIFY_CACHE_SIZE = 8


@dataclass(frozen=True)
class TaskProfile:
    """The task's reasoning mode and the enrichment budget it earns.

    Frozen so a downstream layer cannot quietly retune the budget mid-request.
    Every field has a named consumer, except ``confidence`` and ``signals``,
    which are diagnostics for the debug log and eval triage.
    """

    mode: TaskMode
    tier: Tier
    depth_score: int
    skill_pool_size: int
    skill_render: SkillRender
    implant_budget: int
    suppress_persona_format: bool
    # Diagnostic only, deliberately: it is emitted in the debug log so a bad
    # classification can be triaged, and nothing branches on it. An earlier
    # docstring implied a caller would fall back on low confidence; no caller
    # does, and inventing one would add an untested path.
    confidence: float
    signals: tuple[str, ...] = ()

    @property
    def cache_token(self) -> str:
        """Colon-free slug for the session cache key.

        ``server._load_and_enrich`` keys the prompt cache on
        ``f"{agent}:{query_hash}:{tier}"``. Once render mode and pool size are
        decoupled from the tier, two profiles can share a tier and still build
        different prompts, so the key has to carry the budget too. Colons are
        excluded to preserve the documented three-segment shape; ``confidence``
        is excluded because it is diagnostic, not prompt-affecting.
        """
        return (
            f"{self.tier}|{self.mode}|{self.skill_pool_size}"
            f"|{self.skill_render}|{self.implant_budget}"
            f"|{int(self.suppress_persona_format)}"
        )

    def with_tier(self, tier: Tier) -> "TaskProfile":
        """Return a copy re-pinned to *tier*, re-deriving the budget from it.

        Used by the two callers that must honour an authority outside this
        module: the meta-query ``explicit_tier`` override and the
        ``preferred_implants`` promotion. Re-deriving rather than only swapping
        the label keeps the tier and the budget from disagreeing.
        """
        if tier == self.tier:
            return self
        budget = _TIER_BUDGET.get(tier)
        if budget is None:
            raise ValueError(f"Unknown tier: {tier!r}")
        return TaskProfile(
            mode=self.mode,
            tier=tier,
            depth_score=self.depth_score,
            skill_pool_size=budget["skills"],
            skill_render=budget["render"],
            implant_budget=budget["implants"],
            # Format suppression follows the mode, not the budget: a greeting
            # promoted to standard is still a greeting.
            suppress_persona_format=_MODE_POLICY[self.mode]["suppress_format"],
            confidence=self.confidence,
            signals=self.signals + (f"tier_pinned:{tier}",),
        )


# --- Lexicons ---------------------------------------------------------------
# Word-boundary anchored, unlike the legacy _COMPLEX_SIGNALS, whose bare
# substring match fires "deep" on any query merely containing "план",
# "compare", "design" or "review". Stems are grouped by the mode they imply,
# so the same word now selects a *method* instead of only inflating a budget.
# Russian and Spanish stems are included: the golden set is en 90 / ru 10 / es 10.
#
# Every entry below was added to fix a concrete misclassification in the TRAIN
# half of evals/datasets/routing.jsonl. The held-out half was not inspected
# while writing them.


def _lex(words: Sequence[str] = (), stems: Sequence[str] = ()) -> re.Pattern[str]:
    """Build a boundary-anchored alternation.

    *words* are complete words or phrases and get a trailing boundary as well as
    a leading one. *stems* are deliberate prefixes and stay open-ended.

    The distinction is load-bearing, not stylistic. An earlier revision anchored
    only the left side, so `hi` matched "history"/"his"/"highest" and `post`
    matched "postgres" — short queries containing an ordinary word were
    classified `converse` and answered at the lite tier with no skills. A stem is
    only safe when no unrelated longer word shares its prefix: `analy[sz]` and
    `configur` qualify, `compar` (compartment) and `script` (scripture) do not.
    """
    if not words and not stems:
        raise ValueError("_lex needs at least one word or stem")
    parts = []
    if words:
        parts.append(r"(?:" + "|".join(words) + r")(?![\w])")
    if stems:
        parts.append(r"(?:" + "|".join(stems) + r")")
    return re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")", re.IGNORECASE)


#: Unambiguous quantitative intent — enough on its own.
#: Every entry here is a whole word or phrase. The stems this list used to carry
#: broke the safety rule ``_lex`` documents: ``comput[ae]`` matched "computer",
#: ``integral`` matched "integral part", ``deriv`` matched "the derivative of
#: brand equity", ``equation`` matched "equations". Each of those classified plain
#: prose as ``compute``, whose default tier is ``deep`` — four skills, three
#: implants, full bodies. That is the over-provisioning this module exists to
#: remove, so the inflections are spelled out instead.
_COMPUTE_STRONG_LEX = _lex(
    words=(
        "prove", "proves", "proven", "proof", "proofs", "solve for",
        "show your work", "докажи",
        "compute", "computes", "computing", "computation", "computations",
        "calculate", "calculates", "calculating", "calculation", "calculations",
        "derive", "derives", "deriving", "derivation",
        "theorem", "theorems", "equation", "equations",
        "integrate", "integral of", "definite integral", "indefinite integral",
        "рассчитай", "рассчитать", "вычисли", "вычислить", "уравнение",
        "уравнения", "интеграл", "интеграла", "calcula", "calcular", "demuestra",
    ),
)
#: Ambiguous quantitative phrasing: "how many books are in the series" is a
#: lookup, not a computation. Requires _MATHY corroboration.
_COMPUTE_WEAK_LEX = _lex(
    words=("how many", "how much", "percentage", "percentages", "сколько"),
    stems=("probabilit", "вероятност"),
)
#: Numbers used as quantities: an assignment, an operator between digits, or a
#: unit suffix. Distinguishes a real calculation from a number in prose.
_MATHY = re.compile(
    r"(=\s*-?\d|\d\s*[*/^+]\s*\d|\d\s*%|\$\s?\d"
    r"|\d\s*(kg|kpa|mpa|kn|mm|cm|km|hz|mhz|ghz|ms|kw|mol|ml|°|deg|rad|m/s|years?)\b)",
    re.IGNORECASE,
)
_ANALYZE_LEX = _lex(
    words=(
        "compare", "compares", "compared", "comparing", "comparison",
        "comparisons", "compara", "comparar", "audit", "audits", "audited",
        "auditing", "trade-?offs?", "root cause", "why does", "why is",
        "pros and cons", "почему", "сравни",
    ),
    stems=(
        "analy[sz]", "architect", "refactor", "investigat", "critique",
        "diagnos", "optimi[sz]", "evaluat", "assess", "debug",
        "анализ", "архитектур", "рефактор", "аудит", "исследу", "первопричин",
        "диагност", "оптимиз", "analiz", "arquitectur", "investigar",
    ),
)
#: Academic / synthesis register. These queries read as ordinary prose requests
#: but the labels call them deep research. Deliberately EXCLUDES "implications",
#: which on the train half marked a standard-tier question, not a deep one.
_RESEARCH_WORDS = (
    "overview of", "with reference to", "academic paper", "academic-grade",
    "extremely complex", "in-depth", "in depth", "state of the art",
    "critically", "significance of", "подробно разбер",
    "discuss the", "discuss how", "discuss whether", "discussion of",
    "references", "bibliography",
)
_RESEARCH_LEX = _lex(
    # `discuss` and `referenc` were stems and fired `analyze`/`deep` on
    # "Let's discuss lunch" and "For your reference, the deadline moved" — the
    # most common NON-academic uses of both words. Only the academic collocations
    # survive.
    words=_RESEARCH_WORDS,
    stems=("literatur", "synthes", "обзор", "литератур"),
)
_OPERATE_LEX = _lex(
    words=(
        "fix", "fixes", "fixed", "script", "scripts", "scripting", "command",
        "commands", "run the", "set up", "setup", "patch", "patches",
        "upgrade", "upgrades", "rollback", "write a function", "write a class",
        "запусти",
    ),
    stems=(
        "implement", "install", "configur", "deploy", "migrat",
        "реализу", "исправ", "установ", "настро", "разверн", "миграц",
        "скрипт", "команд", "почин", "implementar", "instalar", "configurar",
    ),
)
#: Pasted source code. Deliberately narrow.
#:
#: Third revision. The first used ``\bselect\b.+\bfrom\b`` under ``re.DOTALL``;
#: the second counted three distinct SQL keywords plus a "structural" token. Both
#: read ordinary English as code, because the SQL vocabulary IS ordinary English —
#: ``on``, ``set``, ``from``, ``values``, ``update``, ``having`` — and ``;``, ``*``
#: and ``word.word`` appear in normal prose. No keyword threshold fixes that, so
#: keyword-counted SQL detection is gone rather than tuned again.
#:
#: What remains matches only forms that prose does not produce: a declaration with
#: code punctuation, or an import statement occupying a whole line. A fenced block
#: is already covered — ``_structural_score`` scores ``code_fence`` separately.
#:
#: Trade-off, accepted knowingly: raw SQL pasted without a fence no longer forces
#: ``operate``; it falls to ``retrieve``/``create`` and is merely mis-budgeted.
#: That is strictly better than classifying "Set the meeting on Monday and update
#: the values from the deck" as a systems operation.
_CODE_DECLARATION = re.compile(
    r"(^[ \t]*(?:async\s+)?def\s+\w+\s*\("
    r"|^[ \t]*function\s+\w+\s*\("
    # `class X:` and `function f(` match ordinary English — "Is this device a
    # class 2(b) under the regulation?", "the function f(x) is convex" — which is
    # exactly the traffic that reaches `lawyer` and `medical_expert`. Both now
    # require a line start, where prose does not put them.
    r"|^[ \t]*class\s+\w+\s*[:({]"
    r"|^[ \t]*#include\b"
    r"|^[ \t]*import\s+[a-z_][\w.]*[ \t]*$"          # a whole line, lowercase module
    r"|^[ \t]*from\s+[a-z_][\w.]*\s+import\s+\w)",  # from x import y
    re.MULTILINE,
)


def _looks_like_code(text: str) -> bool:
    """Whether the query contains a pasted code declaration.

    Case-sensitive on purpose: ``Import duties from China rose`` starts a sentence,
    ``import os`` does not. Lower-casing was what let prose through.
    """
    return bool(_CODE_DECLARATION.search(text))


_CREATE_LEX = _lex(
    words=(
        "write", "writes", "wrote", "writing", "compose", "composes",
        "composing", "draft", "drafts", "generate", "generates", "rewrite",
        "translate", "translates", "story", "stories", "poem", "poems",
        "essay", "essays", "letter", "letters", "email", "emails", "article",
        "articles", "post", "posts", "outline", "outlines",
        "напиши", "составь", "сочини", "перевед", "перепиш",
    ),
    stems=(
        "summari", "письмо", "статья", "рассказ", "escrib", "redact", "traduc",
        "resumir",
    ),
)
_EXPLAIN_LEX = _lex(
    words=(
        "how does", "how do", "what is", "what are", "teach", "teaches",
        "tell me about", "difference between",
        "объясни", "опиши", "расскажи", "что такое", "чем отличается",
    ),
    stems=("explain", "describ", "explica"),
)
_RETRIEVE_LEX = _lex(
    words=(
        "list", "lists", "find", "finds", "look ?up", "define", "defines",
        "definition", "when did", "who is", "who was", "where is", "which of",
        "give me the",
        "покажи", "найди", "перечисли", "когда", "кто такой", "где",
        "lista", "encuentra",
    ),
)
_CONVERSE_LEX = _lex(
    words=(
        "hi", "hello", "hey", "thanks", "thank you", "good morning",
        "good evening", "how are you", "who are you", "what can you do",
        "your name", "nice to meet",
        "привет", "здравствуй", "здравствуйте", "спасибо", "добрый день",
        "добрый вечер", "как дела", "кто ты", "что ты умеешь",
        "hola", "gracias", "quién eres",
    ),
)

#: Line-anchored. A bare ``` matched inline backticks in prose, and once a fence
#: began selecting the mode that let "Use ```code``` formatting in your reply"
#: classify as `operate`. Only a fence that opens a line counts.
_CODE_FENCE = re.compile(r"^[ \t]*(?:```|~~~)", re.MULTILINE)
#: Accepts sub-numbered items ("11.1.", "2.3)") as well as flat ones. The flat-only
#: form missed a multi-part exam paper whose items were numbered 11.1 .. 11.7.
_LIST_LINE = re.compile(r"^\s*(\d+(\.\d+)*[.)]|[-*•])\s+\S")
_URL = re.compile(r"https?://")
#: "Make 33 MCQs ... each MCQ should have at least three options" — a bulk
#: structured deliverable, which the labels treat as deep regardless of length.
_BULK_OUTPUT = re.compile(r"\b\d{2,}\s+\w+", re.IGNORECASE)
_EACH_CONSTRAINT = re.compile(r"\beach\b[^.]{0,60}\b(should|must|has to|needs? to)\b", re.IGNORECASE)

#: The legacy signal set, kept verbatim so its behaviour is auditable next to
#: the replacement. It now contributes a single non-decisive point instead of
#: forcing ``deep``. Retained here (rather than left in ``enrichment``) so both
#: paths read the same constant.
_LEGACY_COMPLEX_SIGNALS = re.compile(
    r"(```|```\w|архитектур|рефактор|оптимиз|debug|анализ|исследу|investigate"
    r"|сравни|compare|план|design|ревью|review|audit|deep dive|/deep)",
    re.IGNORECASE,
)


def _structural_score(text: str, signals: list[str]) -> int:
    """Bounded complexity score from shape alone — never from length alone.

    Length contributes at most 2 of the ``INTENT_DEEP_AT`` points needed to
    promote a tier, so a long-but-simple prompt (the failure mode that sent 35
    of 40 bench queries to ``deep``) cannot reach the heaviest tier on size
    alone.
    """
    score = 0
    n = len(text)
    if n > INTENT_VERY_LONG_CHARS:
        score += 2
        signals.append("very_long")
    elif n > INTENT_LONG_CHARS:
        score += 1
        signals.append("long")

    if _CODE_FENCE.search(text):
        score += 1
        signals.append("code_fence")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) >= 4:
        score += 1
        signals.append("multiline")
    if sum(1 for ln in lines if _LIST_LINE.match(ln)) >= 3:
        score += 1
        signals.append("enumerated")
    if text.count("?") >= 3:
        score += 1
        signals.append("multi_question")
    if _URL.search(text):
        score += 1
        signals.append("url")
    if text.count(",") >= 8:
        score += 1
        signals.append("many_entities")
    if _BULK_OUTPUT.search(text) and _EACH_CONSTRAINT.search(text):
        score += 1
        signals.append("bulk_output")
    if _LEGACY_COMPLEX_SIGNALS.search(text):
        score += 1
        signals.append("legacy_signal")
    return score


#: Words that may accompany a greeting without making it a request.
_GREETING_FILLER = re.compile(
    r"(?<![\w])(there|again|all|everyone|folks|team|guys|please|so much|a lot|"
    r"всем|ещё раз|большое|пожалуйста)(?![\w])",
    re.IGNORECASE,
)


def _is_pure_greeting(text: str) -> bool:
    """True only when the message is a greeting and nothing else.

    A length check alone is not enough, and that was a real defect: the earlier
    version accepted any query under ``INTENT_CONVERSE_MAX_CHARS`` that merely
    *contained* a greeting token, so "Hi, compare Postgres vs MySQL" and
    "hey, debug this stack trace" were classified ``converse`` — lite tier, zero
    skills, zero implants, persona output format stripped. That is precisely the
    ``_is_meta_query`` false positive this module exists to remove; it had only
    been fixed for long queries.

    The test is subtractive: delete every greeting token and permitted filler,
    and require that no alphabetic content survives.
    """
    if not text or len(text) > INTENT_CONVERSE_MAX_CHARS:
        return False
    if not _CONVERSE_LEX.search(text):
        return False
    remainder = _CONVERSE_LEX.sub(" ", text)
    remainder = _GREETING_FILLER.sub(" ", remainder)
    # Alphanumeric, not merely alphabetic: checking only letters let "hi, 2+2?"
    # and "hi, 1234567 * 89 = ?" through as small talk. This subsumes a _MATHY
    # check — every _MATHY alternative requires a digit — so no second guard.
    return not any(ch.isalnum() for ch in remainder)


def _detect_mode(text: str, signals: list[str]) -> tuple[TaskMode, float]:
    """Pick the reasoning mode by lexicon, most-specific first.

    Order is deliberate: ``compute`` and ``analyze`` are checked before the
    broader ``create``/``explain``/``retrieve`` families, because a query can
    legitimately match several ("write an essay comparing X and Y" is an
    analysis delivered as prose) and the costlier method should win.
    ``converse`` is checked first but only for short inputs, so a long task spec
    that merely opens with "Hi" is not read as small talk — the false positive
    ``_is_meta_query`` used to produce.
    """
    stripped = text.strip()
    if _is_pure_greeting(stripped):
        signals.append("converse_only")
        return "converse", 0.9

    # A fenced block selects `operate`. The accepted trade-off for dropping SQL
    # keyword detection was "a fenced block is already covered", and that was not
    # true: `code_fence` contributes 1 of INTENT_DEEP_AT points and cannot affect
    # the mode, so a fenced traceback plus "help" landed on `retrieve`/`lite` with
    # zero skills where the legacy rule gave `deep`. Now it pays for the trade-off.
    if _CODE_FENCE.search(stripped):
        signals.append("code_fence_mode")
        return "operate", 0.8

    mathy = bool(_MATHY.search(stripped))
    if _COMPUTE_STRONG_LEX.search(stripped):
        signals.append("compute_strong")
        return "compute", 0.85
    if _COMPUTE_WEAK_LEX.search(stripped) and mathy:
        signals.append("compute_weak+mathy")
        return "compute", 0.6
    if _ANALYZE_LEX.search(stripped):
        signals.append("analyze_lex")
        return "analyze", 0.75
    if _RESEARCH_LEX.search(stripped):
        signals.append("research_lex")
        return "analyze", 0.7
    if _looks_like_code(stripped):
        signals.append("code_ish")
        return "operate", 0.8
    for mode, lex in (
        ("operate", _OPERATE_LEX),
        ("create", _CREATE_LEX),
        ("explain", _EXPLAIN_LEX),
        ("retrieve", _RETRIEVE_LEX),
    ):
        if lex.search(stripped):
            signals.append(f"{mode}_lex")
            return mode, 0.75

    signals.append("no_lex")
    # No lexical evidence: short inputs are lookups, longer ones read as asks
    # for prose. Lowest confidence, so a caller may prefer the legacy fallback.
    if len(stripped) <= INTENT_LONG_CHARS:
        return "retrieve", 0.4
    return "create", 0.4


def _resolve_tier(mode: TaskMode, score: int) -> Tier:
    """Promote the mode's default budget by at most one step, per the score.

    The mode's default tier is a FLOOR, never lowered. An earlier revision also
    demoted on a low score; measured on the train half that was strictly
    harmful — it sent "Explain University level Introductory Statistics to me
    like I'm a child" to ``lite``, stripping the skills that request needs.
    A mode that deserves ``lite`` says so through its own default.
    """
    default = _MODE_POLICY[mode]["tier"]
    idx = _TIER_ORDER.index(default)
    if score >= INTENT_DEEP_AT:
        idx += 1
    return _TIER_ORDER[min(len(_TIER_ORDER) - 1, idx)]


@lru_cache(maxsize=_CLASSIFY_CACHE_SIZE)
def _classify_cached(text: str) -> TaskProfile:
    """Memoized core. Safe to cache: the function is pure and TaskProfile frozen.

    ``route_and_load`` classifies the same string up to three times per request
    (the ROUTE_REQUIRED payload, ``_load_and_enrich``'s tier, then the profile),
    synchronously on the event loop, and the scan is linear in query length: on
    this checkout a 100 KB query costs ~12 ms per pass. A query carrying a pasted
    file is an ordinary MCP payload, so the repeats are the problem, not the scan.
    The cache is small because the keys are whole queries.
    """
    signals: list[str] = []
    mode, confidence = _detect_mode(text, signals)
    score = _structural_score(text, signals)
    tier = _resolve_tier(mode, score)
    budget = _TIER_BUDGET[tier]
    return TaskProfile(
        mode=mode,
        tier=tier,
        depth_score=score,
        # Budget follows the resolved tier, method follows the mode.
        skill_pool_size=budget["skills"],
        skill_render=budget["render"],
        implant_budget=budget["implants"],
        suppress_persona_format=_MODE_POLICY[mode]["suppress_format"],
        confidence=confidence,
        signals=tuple(signals),
    )


def classify_intent(
    query: str,
    *,
    history: Optional[Sequence[str]] = None,
) -> TaskProfile:
    """Classify *query* into a :class:`TaskProfile`. Pure, sync, no I/O.

    *history* is accepted so callers need not change when conversational
    context starts contributing, and is deliberately unused today: using it
    would make the profile depend on turn order, which the session prompt cache
    key in ``server._load_and_enrich`` does not model.
    """
    text = (query or "").strip()
    if not text:
        return TaskProfile(
            mode="converse", tier="lite", depth_score=0, skill_pool_size=0,
            skill_render=_TIER_BUDGET["lite"]["render"], implant_budget=0,
            suppress_persona_format=True, confidence=1.0, signals=("empty",),
        )
    return _classify_cached(text)
