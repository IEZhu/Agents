#!/usr/bin/env python3
"""Auto-extract keywords from skill files for capable-routing.

Conservative strategy (quality > quantity):
  1. Compiled field: tokenize by period/semicolon, take the meaningful chunks.
  2. Body **bolded** terms: these are conceptually significant.
  3. Description (first sentence only, role-stripped): noun phrases.
  4. Acronyms (uppercase 2-6 chars).

Filter: 3+ chars per word, phrase 4-40 chars, no leading/trailing stopwords,
no numeric fragments, no obvious noise tokens.
"""
from __future__ import annotations

import re
import sys
import yaml
from pathlib import Path

STOP = {
    "the", "a", "an", "and", "or", "but", "of", "in", "on", "for", "to", "with",
    "by", "as", "is", "are", "be", "this", "that", "these", "those", "it", "its",
    "role", "rules", "actions", "concepts", "principles", "best", "practices",
    "tips", "example", "examples", "when", "use", "uses",
    "i", "you", "we", "they", "your", "our", "if", "then", "else",
    "do", "not", "no", "yes", "etc", "ie", "eg", "vs", "via", "from", "at",
    "into", "out", "up", "down", "over", "under", "off", "all", "any", "some",
    "more", "less", "most", "least", "very", "just", "only", "also", "even",
    "such", "same", "other", "another", "each", "every", "where", "what", "which",
    "who", "whom", "whose", "how", "why",
}

# Single-word phrases must be in this whitelist (acronyms or established terms)
SINGLE_WORD_OK = {
    "bluf", "mece", "solid", "dry", "kiss", "yagni", "owasp", "rest", "graphql",
    "sql", "nosql", "api", "ux", "ui", "ocr", "pdf", "json", "yaml", "xml",
    "mcp", "cov", "lot", "tot", "react", "smart", "rice", "ftp", "ssh",
    "tdd", "bdd", "ci", "cd", "cli", "tui", "aws", "gcp", "iam", "vpc", "tcp",
    "udp", "dns", "http", "https", "url", "uri", "css", "html", "rag", "llm",
    "ner", "nlp", "wcag", "ada", "gdpr", "pii", "hipaa", "stride", "dread",
    "agnotology", "pedagogy", "telemetry", "observability",
}

BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*")
ACRONYM_RE = re.compile(r"\b([A-Z]{2,6})\b")


def split_frontmatter(text: str) -> tuple[dict | None, str]:
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n(.*)$", text, re.DOTALL)
    if not m:
        return None, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return None, text
    return fm, m.group(2)


def clean_phrase(s: str) -> str:
    s = s.strip().strip(".,:;()[]{}\"'`*").lower()
    # Drop trailing parenthetical "(x)" residue
    s = re.sub(r"\([^)]*\)$", "", s).strip()
    # Strip leading/trailing stopwords
    parts = s.split()
    while parts and parts[0] in STOP:
        parts.pop(0)
    while parts and parts[-1] in STOP:
        parts.pop()
    s = " ".join(parts)
    # Collapse whitespace
    s = re.sub(r"\s+", " ", s)
    return s


def is_good_phrase(phrase: str) -> bool:
    if not phrase:
        return False
    L = len(phrase)
    if L < 4 or L > 50:
        return False
    parts = phrase.split()
    # Single-word: must be acronym or in whitelist
    if len(parts) == 1:
        if phrase in SINGLE_WORD_OK:
            return True
        # Allow if it's an acronym (was uppercase in source)
        return False
    # Multi-word: each word at least 2 chars (allow short like "of"→already filtered)
    if any(len(p) < 2 for p in parts):
        return False
    # No phrase that is 100% stopwords
    if all(p in STOP for p in parts):
        return False
    # Filter phrases that contain weird residue
    if re.search(r"[=<>{}\[\]]", phrase):
        return False
    if re.search(r"\d{2,}", phrase):  # "20+", "404", etc — usually not good keyword
        return False
    if "→" in phrase or "—" in phrase or "..." in phrase:
        return False
    # Phrase must contain at least one non-stopword
    if not any(p not in STOP for p in parts):
        return False
    return True


def extract(fm: dict, body: str) -> list[str]:
    description = fm.get("description", "") or ""
    compiled = fm.get("compiled", "") or ""

    candidates: list[str] = []

    # (1) Compiled: high signal, already distilled. Split on period.
    for chunk in re.split(r"[.;]\s+", compiled):
        phrase = clean_phrase(chunk)
        if is_good_phrase(phrase):
            candidates.append(phrase)

    # (2) Body: **bolded** terms — conceptually significant
    for m in BOLD_RE.finditer(body):
        raw = m.group(1)
        # Drop everything after first ":" — that's usually a header-style intro
        raw = raw.split(":")[0]
        phrase = clean_phrase(raw)
        if is_good_phrase(phrase):
            candidates.append(phrase)

    # (3) Description first sentence (drop role marker)
    desc_first = re.split(r"[.!?]\s+", description)[0]
    desc_first = re.sub(r"\bRole:\s*[^.]+", "", desc_first)
    # Extract 2-4 word noun-phrases
    for m in re.finditer(r"\b([A-Za-z][A-Za-z0-9'-]{2,}(?:\s+[A-Za-z][A-Za-z0-9'-]{2,}){1,3})\b", desc_first):
        phrase = clean_phrase(m.group(1))
        if is_good_phrase(phrase):
            candidates.append(phrase)

    # (4) Acronyms from body
    for m in ACRONYM_RE.finditer(body + " " + description + " " + compiled):
        phrase = m.group(1).lower()
        if phrase in SINGLE_WORD_OK:
            candidates.append(phrase)

    # Dedup case-insensitive, preserve order (earlier source = higher signal)
    seen: dict[str, bool] = {}
    for c in candidates:
        if c not in seen:
            seen[c] = True

    return list(seen.keys())[:10]


def main() -> int:
    skills_dir = Path("skills")
    paths = sorted(skills_dir.glob("skill-*.mdc"))

    proposal: dict[str, list[str]] = {}

    for p in paths:
        text = p.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if fm is None:
            continue
        if "keywords" in fm:
            continue  # already done
        kws = extract(fm, body)
        proposal[p.stem] = kws

    # Print preview
    print(f"# Keyword extraction — {len(proposal)} skills\n")
    for stem, kws in proposal.items():
        avg_len = sum(len(k) for k in kws) / max(len(kws), 1)
        print(f"## {stem}  ({len(kws)} kws, avg {avg_len:.0f} chars)")
        for k in kws:
            print(f"  - {k}")
        print()

    sizes = [len(v) for v in proposal.values()]
    if not sizes:
        print("\n# Totals: 0 skills pending keyword extraction")
        return 0
    print(f"\n# Totals: {len(proposal)} skills; min={min(sizes)}, max={max(sizes)}, avg={sum(sizes) / len(sizes):.1f}")
    print(f"# Skills with < 5 keywords (need manual review):")
    for stem, kws in proposal.items():
        if len(kws) < 5:
            print(f"  - {stem}: {kws}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
