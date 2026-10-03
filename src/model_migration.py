"""One-time switch of an installation's embedding model to the current default.

Installers pinned whatever model was the default when they ran (e5-large,
MiniLM-L12, all-MiniLM-L6) into ``.env`` or the shared service's
``service.json``. When the default changes, ``GENERATION`` goes up and the next
update moves every installation from its current model, whichever it is, to
``DEFAULT_MODEL`` exactly once: the file records the generation it reached, so a
model chosen after the switch (``EMBEDDING_MODEL=intfloat/multilingual-e5-large``)
stays chosen.

The changed fingerprint makes the skill, implant and history stores re-embed and
the router cache reset on the next load (``src/engine/fingerprint.py``).

Stdlib only: ``src/startup.py`` runs ``migrate_env_file`` inside the exclusive
installation lease, before any engine module is imported.

Run ``python -m src.model_migration <path to .env>`` to migrate one file (the
installers do).
"""

import logging
import os
import sys
import tempfile

# The one model setup installs on every machine.
DEFAULT_MODEL = "microsoft/harrier-oss-v1-270m"
# 2: harrier-oss-v1-270m replaced e5-large, MiniLM-L12 and all-MiniLM-L6 (#160).
GENERATION = 2
GENERATION_KEY = "EMBEDDING_MODEL_GENERATION"
MODEL_KEY = "EMBEDDING_MODEL"

logger = logging.getLogger(__name__)


def _value(line: str, key: str):
    """The value of a ``KEY=value`` line, without quotes or an inline comment; None for other lines."""
    stripped = line.strip()
    if stripped.startswith("export "):
        stripped = stripped[len("export "):].lstrip()
    name, separator, value = stripped.partition("=")
    if not separator or name.strip().strip("'\"") != key:
        return None
    value = value.strip()
    if value[:1] in ("'", '"'):
        quote = value[0]
        end = value.find(quote, 1)
        return value[1:end] if end > 0 else value[1:]
    return value.split(" #", 1)[0].split("\t#", 1)[0].strip()


def read_env(path: str) -> tuple[str | None, int]:
    """(EMBEDDING_MODEL, generation) set in the file; (None, 1) for a file without them."""
    model, generation = None, 1
    with open(path, encoding="utf-8") as stream:
        for line in stream:
            if (value := _value(line, MODEL_KEY)) is not None:
                model = value or None  # the last assignment wins, as in python-dotenv
            elif (value := _value(line, GENERATION_KEY)) is not None:
                try:
                    generation = int(value)
                except ValueError:
                    generation = 1
    return model, generation


def _write_atomic(path: str, text: str) -> None:
    """Replace *path* with *text*, keeping its permission bits; readers see the old or the new file."""
    folder = os.path.dirname(os.path.abspath(path))
    mode = os.stat(path).st_mode & 0o7777
    descriptor, staging = tempfile.mkstemp(prefix=".env.", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(staging, mode)
        os.replace(staging, path)
    except BaseException:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
        raise


def migrate_env_file(path: str, environ=None) -> tuple[str | None, str] | None:
    """Move *path* to ``DEFAULT_MODEL`` once; ``(old model, new model)`` when it changed.

    A file that already reached ``GENERATION`` is left alone, as is a missing
    file (the engine default applies). A file without ``EMBEDDING_MODEL`` only
    gets the generation marker. *environ* (``os.environ`` by default) follows the
    file when it holds the value loaded from it; an exported value that differs
    from the file is the caller's explicit choice and stays.
    """
    environ = os.environ if environ is None else environ
    if not os.path.isfile(path):
        return None
    model, generation = read_env(path)
    if generation >= GENERATION:
        return None
    with open(path, encoding="utf-8") as stream:
        lines = stream.readlines()
    switch = model is not None and model != DEFAULT_MODEL
    kept = [line for line in lines
            if _value(line, GENERATION_KEY) is None and not (switch and _value(line, MODEL_KEY) is not None)]
    if kept and not kept[-1].endswith("\n"):
        kept[-1] += "\n"
    if switch:
        kept.append(f"# Switched from {model} by the update to embedding generation {GENERATION}; "
                    f"set {MODEL_KEY} back to keep the old model.\n")
        kept.append(f"{MODEL_KEY}={DEFAULT_MODEL}\n")
    kept.append(f"{GENERATION_KEY}={GENERATION}\n")
    _write_atomic(path, "".join(kept))
    if not switch:
        return None
    if environ.get(MODEL_KEY) in (None, model):
        environ[MODEL_KEY] = DEFAULT_MODEL
    else:
        logger.warning("%s=%s is exported and differs from %s; the exported value stays in effect",
                       MODEL_KEY, environ[MODEL_KEY], path)
    logger.warning("Embedding model switched from %s to %s in %s; the stores re-embed on this start",
                   model, DEFAULT_MODEL, path)
    return model, DEFAULT_MODEL


def service_switch_pending(config: dict) -> bool:
    """True while a shared service's ``service.json`` predates ``GENERATION``."""
    return bool(config) and int(config.get("model_generation") or 1) < GENERATION


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print("usage: python -m src.model_migration <path to .env>", file=sys.stderr)
        return 2
    changed = migrate_env_file(argv[0])
    if changed:
        print(f"Embedding model switched from {changed[0]} to {changed[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
