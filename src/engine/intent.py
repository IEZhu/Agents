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
from typing import Literal, Optional, Sequence

from src.engine.config import (
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
_MODE_POLICY: dict[str, dict] = {
    "converse": {"tier": "lite",     "skills": 0, "implants": 0, "render": "compiled", "suppress_format": True},
    "retrieve": {"tier": "lite",     "skills": 0, "implants": 0, "render": "compiled", "suppress_format": False},
    "create":   {"tier": "standard", "skills": 2, "implants": 2, "render": "compiled", "suppress_format": False},
    "explain":  {"tier": "standard", "skills": 2, "implants": 2, "render": "compiled", "suppress_format": False},
    "operate":  {"tier": "standard", "skills": 2, "implants": 2, "render": "compiled", "suppress_format": False},
    "analyze":  {"tier": "deep",     "skills": 4, "implants": 3, "render": "full",     "suppress_format": False},
    "compute":  {"tier": "deep",     "skills": 4, "implants": 3, "render": "full",     "suppress_format": False},
}

_TIER_ORDER: tuple[Tier, ...] = ("lite", "standard", "deep")


@dataclass(frozen=True)
class TaskProfile:
    """The task's reasoning mode and the enrichment budget it earns.

    Frozen so a downstream layer cannot quietly retune the budget mid-request.
    Every field has exactly one named consumer; nothing here is speculative.
    """

    mode: TaskMode
    tier: Tier
    depth_score: int
    skill_pool_size: int
    skill_render: SkillRender
    implant_budget: int
    suppress_persona_format: bool
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
        for mode, policy in _MODE_POLICY.items():
            if policy["tier"] == tier:
                base = _MODE_POLICY[self.mode]
                return TaskProfile(
                    mode=self.mode,
                    tier=tier,
                    depth_score=self.depth_score,
                    skill_pool_size=policy["skills"],
                    skill_render=policy["render"],
                    implant_budget=policy["implants"],
                    # Format suppression follows the mode, not the budget: a
                    # greeting promoted to standard is still a greeting.
                    suppress_persona_format=base["suppress_format"],
                    confidence=self.confidence,
                    signals=self.signals + (f"tier_pinned:{tier}",),
                )
        raise ValueError(f"Unknown tier: {tier!r}")


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
_COMPUTE_STRONG_LEX = _lex(
    words=(
        "prove", "proves", "proven", "proof", "proofs", "solve for",
        "show your work", "докажи",
    ),
    stems=(
        "calculat", "comput[ae]", "deriv", "theorem", "equation", "integral",
        "рассчита", "вычисл", "уравнени", "интеграл", "calcul", "demuestr",
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
_RESEARCH_LEX = _lex(
    words=(
        "overview of", "with reference to", "academic paper", "academic-grade",
        "extremely complex", "in-depth", "in depth", "state of the art",
        "critically", "significance of", "подробно разбер",
    ),
    stems=(
        "discuss", "literatur", "referenc", "synthes", "обзор", "литератур",
    ),
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
#: Source code or SQL pasted into the query: an operate task even with no verb.
_CODE_ISH = re.compile(
    r"(\bselect\b.+\bfrom\b|\bupdate\b.+\bset\b|\binsert\s+into\b"
    r"|\bjoin\b.+\bon\b|\bdef\s+\w+\s*\(|\bfunction\s+\w+\s*\("
    r"|\bclass\s+\w+\s*[:({]|#include\b|\bimport\s+\w+)",
    re.IGNORECASE | re.DOTALL,
)
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

_CODE_FENCE = re.compile(r"```")
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
    if len(stripped) <= INTENT_CONVERSE_MAX_CHARS and _CONVERSE_LEX.search(stripped):
        signals.append("converse_lex")
        return "converse", 0.9

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
    if _CODE_ISH.search(stripped):
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
    signals: list[str] = []
    if not text:
        return TaskProfile(
            mode="converse", tier="lite", depth_score=0, skill_pool_size=0,
            skill_render="compiled", implant_budget=0,
            suppress_persona_format=True, confidence=1.0, signals=("empty",),
        )

    mode, confidence = _detect_mode(text, signals)
    score = _structural_score(text, signals)
    tier = _resolve_tier(mode, score)
    policy = _MODE_POLICY[mode]
    tier_policy = next(p for p in _MODE_POLICY.values() if p["tier"] == tier)

    return TaskProfile(
        mode=mode,
        tier=tier,
        depth_score=score,
        # Budget follows the resolved tier, method follows the mode.
        skill_pool_size=tier_policy["skills"],
        skill_render=tier_policy["render"],
        implant_budget=tier_policy["implants"],
        suppress_persona_format=policy["suppress_format"],
        confidence=confidence,
        signals=tuple(signals),
    )
