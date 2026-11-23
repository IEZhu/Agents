"""Statements a repository snapshot decides, their perturbations and their deterministic checks.

The strata are the `no-fabrication` claim types a snapshot can decide. Each extractor reads one
file and returns `Fact`s: a true statement about that file and the false variants it can be
perturbed into, copying real errors (a near-miss name, an off-by-one or wrong-unit number, a
wrong default, a path one segment off, a negated condition, a value true at an earlier commit).
`check` decides a statement's subject again from the snapshot, independently of how it was
built, and returns ``None`` when the file does not settle it; such a statement goes to the
owner, never into the set.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import hashlib
import re
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

STRATA = ("name_signature", "cli_flag", "config_default", "path", "numeric_constant", "doc_quote",
          "pinned_version")
# A single command a verifier would plausibly run (grep, test -e, python -c) settles these. A quoted
# sentence does not: Markdown wraps it across lines, so it is decided by reading.
COMMAND_CHECKABLE = frozenset(STRATA) - {"doc_quote"}
PERTURBATIONS = ("near_miss_name", "off_by_one", "wrong_unit", "wrong_default", "path_segment",
                 "negated", "earlier_commit")

MAX_FILE_BYTES = 400_000
MAX_LOCK_BYTES = 2_000_000
_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]{2,}")
_ENV_KEY = re.compile(r"[A-Z][A-Z0-9_]{2,}")
_PIN = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([0-9][0-9A-Za-z.+!-]*)\s*(?:[;#].*)?$")
_USES = re.compile(r"^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+)@(v\d+(?:\.\d+)*)\s*(?:#.*)?$")
_LOCK_PACKAGE = re.compile(r'^\[\[package\]\]\s*\nname = "([^"]+)"\s*\nversion = "([^"]+)"', re.MULTILINE)
_WORD = re.compile(r"[a-z]{4,}")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z`*_(\"'])")
# Negations in the order they are tried; the first that applies is used once.
NEGATIONS = (
    (r"\bmust not\b", "must"), (r"\bmust\b", "must not"), (r"\bnever\b", "always"),
    (r"\balways\b", "never"), (r"\bcannot\b", "can"), (r"\bcan\b", "cannot"),
    (r"\bdoes not\b", "does"), (r"\bdo not\b", "do"), (r"\bis not\b", "is"), (r"\bare not\b", "are"),
    (r"\bis\b", "is not"), (r"\bare\b", "are not"), (r"\bbefore\b", "after"), (r"\bafter\b", "before"),
)
_DEFAULT_FLIPS = {"1": "0", "0": "1", "true": "false", "false": "true", "on": "off", "off": "on",
                  "yes": "no", "no": "yes"}


@dataclass(frozen=True)
class Claim:
    """A statement and the subject its check needs."""

    stratum: str
    path: str  # the file the statement is about, relative to the repository root
    subject: tuple  # hashable, JSON-friendly key/value pairs
    statement: str

    def subject_dict(self) -> dict:
        return dict(self.subject)


@dataclass(frozen=True)
class Variant:
    kind: str
    claim: Claim


@dataclass
class Fact:
    """A true claim about one file and the false variants available for it."""

    claim: Claim
    variants: list[Variant] = field(default_factory=list)
    # Values the same subject had at earlier commits: built lazily, only for chosen facts.
    history: Callable[[], Iterable[Variant]] | None = None


def cluster_of(path: str) -> str:
    """The source directory two levels deep, ``.`` for the repository root."""
    parts = PurePosixPath(path).parent.parts
    return "/".join(parts[:2]) if parts else "."


def _items(subject: dict) -> tuple:
    return tuple(sorted(subject.items()))


# --- Python sources -------------------------------------------------------------------------


def _parse(text: str) -> ast.Module | None:
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError):
        return None


def _number(node) -> int | float | None:
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    if (isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant)
            and type(node.operand.value) in (int, float)):
        return -node.operand.value
    return None


def _module_assignments(tree: ast.Module) -> dict[str, list]:
    """Every module-level assignment by name: its number, or ``...`` for any other value."""
    found: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                number = _number(value)
                found.setdefault(target.id, []).append(... if number is None else number)
    return found


def _render_number(value) -> str:
    return repr(value) if isinstance(value, float) else str(value)


def _constant_claim(path: str, name: str, value) -> Claim:
    return Claim("numeric_constant", path, _items({"name": name, "value": value}),
                 f"In `{path}`, the module-level constant `{name}` is set to `{_render_number(value)}`.")


def _numbers_off(value) -> list[tuple[str, int | float]]:
    out = []
    if isinstance(value, int):
        out.append(("off_by_one", value + 1 if value >= 0 else value - 1))
        if value:
            out.append(("wrong_unit", value * 1000 if abs(value) < 10 ** 6 else value // 1000))
    else:
        out.append(("wrong_unit", round(value * 100, 6) if abs(value) < 1 else round(value * 1000, 6)))
    return out


def numeric_constants(path: str, text: str, tree: ast.Module, earlier) -> list[Fact]:
    facts = []
    for name, values in _module_assignments(tree).items():
        if not _CONSTANT.fullmatch(name) or len(values) != 1 or values[0] is ...:
            continue
        value = values[0]
        claim = _constant_claim(path, name, value)
        variants = [Variant(kind, _constant_claim(path, name, wrong)) for kind, wrong in _numbers_off(value)]

        def history(name=name, value=value):
            for old in earlier(path):
                tree_then = _parse(old)
                then = _module_assignments(tree_then).get(name, []) if tree_then else []
                if len(then) == 1 and then[0] is not ... and then[0] != value:
                    yield Variant("earlier_commit", _constant_claim(path, name, then[0]))
                    return
        facts.append(Fact(claim, variants, history))
    return facts


def _is_env_get(func) -> bool:
    if isinstance(func, ast.Attribute) and func.attr == "get":
        target = func.value
        return ((isinstance(target, ast.Attribute) and target.attr == "environ")
                or (isinstance(target, ast.Name) and target.id == "environ"))
    if isinstance(func, ast.Attribute) and func.attr == "getenv":
        return isinstance(func.value, ast.Name) and func.value.id == "os"
    return isinstance(func, ast.Name) and func.id == "getenv"


def _literal(node):
    if isinstance(node, ast.Constant) and type(node.value) in (str, int, float):
        return node.value
    return _number(node)


def _env_reads(tree: ast.Module) -> dict[str, list]:
    """Defaults per environment key: the literal, ``None`` without one, ``...`` for an expression."""
    reads: dict[str, list] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_env_get(node.func) and node.args):
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        if len(node.args) >= 2:
            default = _literal(node.args[1])
            reads.setdefault(key.value, []).append(... if default is None else default)
        elif any(keyword.arg == "default" for keyword in node.keywords):
            reads.setdefault(key.value, []).append(...)
        else:
            reads.setdefault(key.value, []).append(None)
    return reads


def _default_claim(path: str, key: str, default) -> Claim:
    return Claim("config_default", path, _items({"key": key, "default": default}),
                 f"In `{path}`, the environment variable `{key}` is read with the default value `{default!r}`.")


def _wrong_defaults(default) -> list:
    if isinstance(default, str):
        flipped = _DEFAULT_FLIPS.get(default.lower())
        if flipped is not None:
            return [flipped]
        if default.isdigit():
            return [str(int(default) + 1)]
        return [default[:-1] if len(default) > 1 else default + "s"] if default else ["0"]
    return [value for _, value in _numbers_off(default)]


def config_defaults(path: str, text: str, tree: ast.Module, earlier) -> list[Fact]:
    facts = []
    for key, defaults in _env_reads(tree).items():
        literal = [value for value in defaults if value is not None and value is not ...]
        if not _ENV_KEY.fullmatch(key) or not literal or ... in defaults:
            continue
        default = literal[0]
        variants = [Variant("wrong_default", _default_claim(path, key, wrong))
                    for wrong in _wrong_defaults(default) if wrong not in defaults]

        def history(key=key, defaults=tuple(defaults)):
            for old in earlier(path):
                tree_then = _parse(old)
                then = _env_reads(tree_then).get(key, []) if tree_then else []
                for value in then:
                    if value is not None and value is not ... and value not in defaults:
                        yield Variant("earlier_commit", _default_claim(path, key, value))
                        return
        facts.append(Fact(_default_claim(path, key, default), variants, history))
    return facts


def _signature(function) -> str:
    arguments, parts = function.args, []
    parts += [argument.arg for argument in arguments.posonlyargs]
    if arguments.posonlyargs:
        parts.append("/")
    parts += [argument.arg for argument in arguments.args]
    if arguments.vararg:
        parts.append("*" + arguments.vararg.arg)
    elif arguments.kwonlyargs:
        parts.append("*")
    parts += [argument.arg for argument in arguments.kwonlyargs]
    if arguments.kwarg:
        parts.append("**" + arguments.kwarg.arg)
    return ", ".join(parts)


def _top_level_names(tree: ast.Module) -> dict[str, list]:
    names: dict[str, list] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.setdefault(node.name, []).append(node)
        elif isinstance(node, ast.ClassDef):
            names.setdefault(node.name, []).append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, []).append(node)
    return names


def _signature_claim(path: str, name: str, signature: str) -> Claim:
    return Claim("name_signature", path, _items({"name": name, "signature": signature}),
                 f"`{path}` defines a top-level function with the signature `{name}({signature})`, "
                 "ignoring annotations and default values.")


def near_misses(name: str) -> list[str]:
    """Plausible misspellings: a toggled plural, a dropped letter, two swapped letters."""
    out = [name[:-1] if name.endswith("s") and len(name) > 4 else name + "s"]
    for match in _WORD.finditer(name):
        start, end = match.span()
        word = match.group()
        middle = start + len(word) // 2
        out.append(name[:middle] + name[middle + 1:])  # a dropped letter
        if word[len(word) // 2] != word[len(word) // 2 - 1]:
            swapped = name[:middle - 1] + name[middle] + name[middle - 1] + name[middle + 1:]
            out.append(swapped)
    seen, unique = {name}, []
    for candidate in out:
        if candidate not in seen and candidate.strip("_-"):
            seen.add(candidate)
            unique.append(candidate)
    return unique


def signatures(path: str, text: str, tree: ast.Module, earlier) -> list[Fact]:
    names = _top_level_names(tree)
    facts = []
    for name, nodes in names.items():
        if name.startswith("_") or len(nodes) != 1 or not isinstance(nodes[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        signature = _signature(nodes[0])
        variants = [Variant("near_miss_name", _signature_claim(path, wrong, signature))
                    for wrong in near_misses(name) if wrong not in names]
        parameters = [part for part in signature.split(", ") if part and part not in ("/", "*")]
        if parameters:  # a near-miss name of the last parameter
            bare = [part.lstrip("*") for part in parameters]
            for wrong in near_misses(bare[-1]):
                if wrong not in bare:
                    variants.append(Variant("near_miss_name", _signature_claim(
                        path, name, _rename_last(signature, parameters[-1], wrong))))
                    break
        facts.append(Fact(_signature_claim(path, name, signature), variants))
    return facts


def _rename_last(signature: str, parameter: str, wrong: str) -> str:
    stars = parameter[:len(parameter) - len(parameter.lstrip("*"))]
    parts = signature.split(", ")
    parts[len(parts) - 1 - parts[::-1].index(parameter)] = stars + wrong
    return ", ".join(parts)


def _flags(tree: ast.Module) -> set[str]:
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str) \
                        and argument.value.startswith("--") and len(argument.value) > 3:
                    flags.add(argument.value)
    return flags


def _flag_claim(path: str, flag: str) -> Claim:
    return Claim("cli_flag", path, _items({"flag": flag}),
                 f"`{path}` defines the command-line option `{flag}`.")


def cli_flags(path: str, text: str, tree: ast.Module, earlier) -> list[Fact]:
    flags = _flags(tree)
    facts = []
    for flag in sorted(flags):
        variants = [Variant("near_miss_name", _flag_claim(path, "--" + wrong))
                    for wrong in near_misses(flag[2:]) if "--" + wrong not in flags and "--" + wrong not in text]
        facts.append(Fact(_flag_claim(path, flag), variants))
    return facts


PYTHON_EXTRACTORS = {"numeric_constant": numeric_constants, "config_default": config_defaults,
                     "name_signature": signatures, "cli_flag": cli_flags}


# --- Documentation ---------------------------------------------------------------------------


def normalize(text: str) -> str:
    return " ".join(text.split())


def _paragraphs(text: str) -> list[str]:
    paragraphs, current, fenced = [], [], False
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # front matter
        end = next((index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
        lines = lines[end + 1:]
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            current = []
            continue
        # Only plain prose paragraphs: code, headings, tables, quotes, HTML, lists and their
        # indented continuations end a paragraph and start none.
        if (fenced or not stripped or line[:1].isspace() or re.match(r"\d+\.\s", stripped)
                or stripped.startswith(("#", "|", ">", "<", "- ", "* ", "+ "))):
            if current:
                paragraphs.append(" ".join(current))
            current = []
            continue
        current.append(stripped)
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _sentences(text: str) -> list[str]:
    out = []
    for paragraph in _paragraphs(text):
        for sentence in _SENTENCE_SPLIT.split(paragraph):
            words = sentence.split()
            if (8 <= len(words) <= 35 and sentence.endswith(".") and (sentence[0].isupper() or sentence[0] == "`")
                    and not re.search(r"[\[\]|<>\\]|http", sentence)
                    and sentence.count("`") % 2 == 0 and '"' not in sentence):
                out.append(sentence)
    return out


def negate(sentence: str) -> str | None:
    for pattern, replacement in NEGATIONS:
        changed, count = re.subn(pattern, replacement, sentence, count=1)
        if count:
            return changed
    return None


def _quote_claim(path: str, sentence: str) -> Claim:
    return Claim("doc_quote", path, _items({"sentence": sentence}),
                 f"`{path}` contains the following sentence, ignoring line breaks and repeated spaces: "
                 f"\"{sentence}\"")


def doc_quotes(path: str, text: str, earlier) -> list[Fact]:
    whole = normalize(text)
    facts = []
    for sentence in _sentences(text):
        negated = negate(sentence)
        if negated is None or normalize(negated) in whole:
            continue
        facts.append(Fact(_quote_claim(path, sentence), [Variant("negated", _quote_claim(path, negated))]))
    return facts


# --- Pinned versions -------------------------------------------------------------------------


def _bump(version: str) -> str | None:
    match = re.search(r"(\d+)(?!.*\d)", version)
    if not match:
        return None
    return version[:match.start()] + str(int(match.group()) + 1) + version[match.end():]


def _pins(path: str, text: str) -> dict[str, set[str]]:
    """Name to pinned versions in a requirements file, a workflow or a uv lock file."""
    pins: dict[str, set[str]] = {}
    name = PurePosixPath(path).name
    if name.endswith(".lock"):
        for package, version in _LOCK_PACKAGE.findall(text):
            pins.setdefault(package, set()).add(version)
        return pins
    pattern = _PIN if name.endswith(".txt") else _USES
    for line in text.splitlines():
        match = pattern.match(line)
        if match:
            pins.setdefault(match.group(1), set()).add(match.group(2))
    return pins


def _pin_claim(path: str, package: str, version: str) -> Claim:
    name = PurePosixPath(path).name
    if name.endswith(".lock"):
        text = f"`{path}` locks the package `{package}` at version `{version}`."
    elif name.endswith(".txt"):
        text = f"`{path}` pins the package `{package}` to version `{version}`."
    else:
        text = f"`{path}` uses the GitHub action `{package}` at the version tag `{version}`."
    return Claim("pinned_version", path, _items({"package": package, "version": version}), text)


def pinned_versions(path: str, text: str, earlier) -> list[Fact]:
    pins = _pins(path, text)
    facts = []
    for package in sorted(pins):
        if len(pins[package]) != 1:
            continue
        version = next(iter(pins[package]))
        bumped = _bump(version)
        variants = [Variant("off_by_one", _pin_claim(path, package, bumped))] if bumped else []

        def history(package=package, version=version):
            for old in earlier(path):
                then = _pins(path, old).get(package, set())
                if len(then) == 1 and version not in then:
                    yield Variant("earlier_commit", _pin_claim(path, package, next(iter(then))))
                    return
        facts.append(Fact(_pin_claim(path, package, version), variants, history))
    return facts


def is_pin_file(path: str) -> bool:
    pure = PurePosixPath(path)
    return (pure.name == "uv.lock" or (pure.name.startswith("requirements") and pure.suffix == ".txt")
            or (pure.parts[:2] == (".github", "workflows") and pure.suffix in (".yml", ".yaml")))


# --- Paths -----------------------------------------------------------------------------------


def _path_claim(path: str) -> Claim:
    return Claim("path", path, _items({"target": path}),
                 f"The repository contains a file at the path `{path}`.")


def paths(path: str, directories: set[str], files: set[str]) -> list[Fact]:
    """The file exists; a false variant moves it one directory segment, into a real sibling.

    A file at the root moves into a top-level directory. Which sibling is fixed by the path's
    hash, so files of one directory do not all move to the same one.
    """
    pure = PurePosixPath(path)
    parent = pure.parent
    if str(parent) == ".":
        siblings = sorted(directory for directory in directories if "/" not in directory)
    else:
        siblings = sorted(directory for directory in directories
                          if PurePosixPath(directory).parent == parent.parent and directory != str(parent))
    variants = []
    start = int(hashlib.sha256(path.encode()).hexdigest(), 16) % len(siblings) if siblings else 0
    for sibling in siblings[start:] + siblings[:start]:
        moved = f"{sibling}/{pure.name}"
        if moved not in files:
            variants.append(Variant("path_segment", _path_claim(moved)))
            break
    return [Fact(_path_claim(path), variants)]


# --- Extraction and checks -------------------------------------------------------------------


def extract(path: str, text: str, *, directories: set[str], files: set[str],
            earlier: Callable[[str], Iterable[str]]) -> dict[str, list[Fact]]:
    """The facts each stratum offers for one file."""
    found: dict[str, list[Fact]] = {"path": paths(path, directories, files)}
    suffix = PurePosixPath(path).suffix
    if suffix == ".py":
        tree = _parse(text)
        if tree is not None:
            for stratum, extractor in PYTHON_EXTRACTORS.items():
                found[stratum] = extractor(path, text, tree, earlier)
    elif suffix == ".md":
        found["doc_quote"] = doc_quotes(path, text, earlier)
    if is_pin_file(path):
        found["pinned_version"] = pinned_versions(path, text, earlier)
    return {stratum: facts for stratum, facts in found.items() if facts}


def check(claim: Claim, snapshot: Path) -> bool | None:
    """Decide ``claim`` from the snapshot's files alone; ``None`` when they do not settle it."""
    subject = claim.subject_dict()
    if claim.stratum == "path":
        return (snapshot / subject["target"]).is_file()
    file = snapshot / claim.path
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    if claim.stratum == "doc_quote":
        return normalize(subject["sentence"]) in normalize(text)
    if claim.stratum == "pinned_version":
        pins = _pins(claim.path, text).get(subject["package"])
        if not pins:
            return False
        return subject["version"] in pins if len(pins) == 1 else None
    tree = _parse(text)
    if tree is None:
        return None
    if claim.stratum == "numeric_constant":
        values = _module_assignments(tree).get(subject["name"], [])
        if not values:
            return False
        if len(values) != 1 or values[0] is ...:
            return None
        value = values[0]
        return type(value) is type(subject["value"]) and value == subject["value"]
    if claim.stratum == "config_default":
        defaults = _env_reads(tree).get(subject["key"], [])
        wanted = subject["default"]
        if any(value is not ... and type(value) is type(wanted) and value == wanted for value in defaults):
            return True
        return None if ... in defaults else False  # an expression default could equal it
    if claim.stratum == "name_signature":
        nodes = _top_level_names(tree).get(subject["name"], [])
        if not nodes:
            return False
        if len(nodes) != 1 or not isinstance(nodes[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            return None
        return _signature(nodes[0]) == subject["signature"]
    if claim.stratum == "cli_flag":
        flags = _flags(tree)
        if subject["flag"] in flags:
            return True
        return None if subject["flag"] in text else False
    raise ValueError(f"unknown stratum {claim.stratum!r}")
