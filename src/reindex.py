"""Rebuild the skills/implants vector stores from the .mdc source files.

Run manually with ``python -m src.reindex`` after editing skills/implants, or
automatically by the background auto-updater (``src/self_update.py``):

  * Phase B (staged, the default) runs it with ``cwd`` set to a fresh git
    worktree of the target commit, so ``config.INSTALL_DATA_DIR`` resolves inside
    that worktree (``-m`` puts cwd on ``sys.path``) and the stores are built there,
    isolated from the live install. ``EMBEDDING_MODEL`` is passed through the
    subprocess env so the staged vectors match the live process.
  * The legacy in-place path runs it with ``cwd=repo_root`` right after a
    fast-forward, so the *next* server start finds the indexes already built.

Constructing ``SkillRetriever`` / ``ImplantRetriever`` is all that's needed:
their ``__init__`` compares a content hash (EMBEDDING_MODEL + every ``.mdc``
file) against the stored ``.skills_hash`` / ``.implants_hash`` and re-embeds
only when something changed. The router cache invalidates itself on model
change at server startup, so it needs no action here.
"""

import logging
import os
import sys

import dotenv

# Make ``src`` importable both as ``python -m src.reindex`` and as a direct
# script — mirrors src/server.py.
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load .env so EMBEDDING_MODEL et al. match the server's configuration even
# when this module is run standalone (subprocess invocations inherit the
# parent env, but a manual run would not).
dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "../.env"))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("reindex")


def main() -> int:
    """Reindex skills and implants. Returns a process exit code (0 = success)."""
    from src.engine.skills import SkillRetriever
    from src.engine.implants import ImplantRetriever

    logger.info("Reindex: building skills store...")
    skills = SkillRetriever()
    logger.info("Reindex: skills store ready (%d entries)", skills.store.count())

    logger.info("Reindex: building implants store...")
    implants = ImplantRetriever()
    logger.info("Reindex: implants store ready (%d entries)", implants.store.count())

    logger.info("Reindex complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
