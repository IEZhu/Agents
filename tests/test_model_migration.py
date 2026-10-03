"""One-time switch of installed embedding models to the current default (src/model_migration.py)."""
import os
import stat
import sys

import pytest
from dotenv import dotenv_values

from src import model_migration as mm
from src import startup

E5 = "intfloat/multilingual-e5-large"


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


@pytest.mark.parametrize("old", [E5, "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
                                 "sentence-transformers/all-MiniLM-L6-v2", "some/custom-model"])
def test_any_configured_model_switches_to_the_default_once(tmp_path, old):
    env = write(tmp_path / ".env", f"LANGFUSE_HOST=https://x\nEMBEDDING_MODEL={old}\nAGENTS_DEBUG=0  # note\n")
    environ = {"EMBEDDING_MODEL": old}

    assert mm.migrate_env_file(str(env), environ) == (old, mm.DEFAULT_MODEL)

    values = dotenv_values(env)
    assert values["EMBEDDING_MODEL"] == mm.DEFAULT_MODEL
    assert values[mm.GENERATION_KEY] == str(mm.GENERATION)
    assert values["LANGFUSE_HOST"] == "https://x" and values["AGENTS_DEBUG"] == "0"
    assert f"# Switched from {old}" in env.read_text()
    assert environ["EMBEDDING_MODEL"] == mm.DEFAULT_MODEL  # the value loaded from the file follows it
    # A model chosen after the switch stays chosen.
    env.write_text(env.read_text().replace(f"EMBEDDING_MODEL={mm.DEFAULT_MODEL}", f"EMBEDDING_MODEL={E5}"))
    assert mm.migrate_env_file(str(env), {}) is None
    assert dotenv_values(env)["EMBEDDING_MODEL"] == E5


def test_quoted_exported_and_repeated_assignments_are_recognized(tmp_path):
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL=old/one\nexport 'EMBEDDING_MODEL' = \"{E5}\"  # pinned")
    assert mm.read_env(str(env)) == (E5, 1)  # the last assignment wins, as in python-dotenv
    assert mm.migrate_env_file(str(env), {}) == (E5, mm.DEFAULT_MODEL)
    text = env.read_text()
    assert "old/one" not in text.replace("# Switched", "")
    assert dotenv_values(env)["EMBEDDING_MODEL"] == mm.DEFAULT_MODEL


def test_file_without_a_model_only_gets_the_marker(tmp_path):
    env = write(tmp_path / ".env", "AGENTS_AUTO_UPDATE=0")
    assert mm.migrate_env_file(str(env), {}) is None
    assert env.read_text() == f"AGENTS_AUTO_UPDATE=0\n{mm.GENERATION_KEY}={mm.GENERATION}\n"
    assert mm.migrate_env_file(str(tmp_path / "missing.env"), {}) is None
    assert not (tmp_path / "missing.env").exists()


def test_exported_model_that_differs_from_the_file_stays(tmp_path):
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL={E5}\n")
    environ = {"EMBEDDING_MODEL": "chosen/by-export"}
    mm.migrate_env_file(str(env), environ)
    assert environ["EMBEDDING_MODEL"] == "chosen/by-export"


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_permissions_survive_the_rewrite(tmp_path):
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL={E5}\n")
    env.chmod(0o600)
    mm.migrate_env_file(str(env), {})
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir()] == [".env"]


def test_cli_migrates_the_named_file(tmp_path, capsys, monkeypatch):
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL={E5}\n")
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    assert mm.main([str(env)]) == 0
    assert "switched from intfloat/multilingual-e5-large" in capsys.readouterr().out
    assert mm.main([]) == 2


def test_service_switch_is_pending_until_the_config_records_the_generation():
    assert mm.service_switch_pending({"model": E5})
    assert not mm.service_switch_pending({"model": E5, "model_generation": mm.GENERATION})
    assert not mm.service_switch_pending({})


def test_engine_default_is_the_migration_target(monkeypatch):
    import importlib
    from src.engine import config
    monkeypatch.delenv("EMBEDDING_MODEL", raising=False)
    try:
        assert importlib.reload(config).EMBEDDING_MODEL == mm.DEFAULT_MODEL
        monkeypatch.setenv("EMBEDDING_MODEL", "")  # the blank env.example line counts as unset
        assert importlib.reload(config).EMBEDDING_MODEL == mm.DEFAULT_MODEL
    finally:
        monkeypatch.undo()
        importlib.reload(config)


def test_startup_migration_restarts_a_process_that_loaded_the_old_model(tmp_path, monkeypatch):
    from src import self_update
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL={E5}\n")
    monkeypatch.setenv("EMBEDDING_MODEL", E5)
    restarts = []
    monkeypatch.setattr(self_update, "_reexec_updated_server", lambda: restarts.append(os.environ["EMBEDDING_MODEL"]))
    assert "src.engine.config" in sys.modules  # activation imported it through self_update

    startup._migrate_model(str(tmp_path))
    startup._migrate_model(str(tmp_path))  # the generation marker ends it after one restart

    assert restarts == [mm.DEFAULT_MODEL]
    assert dotenv_values(env)["EMBEDDING_MODEL"] == mm.DEFAULT_MODEL


def test_startup_keeps_serving_when_the_migration_fails(tmp_path, monkeypatch):
    write(tmp_path / ".env", f"EMBEDDING_MODEL={E5}\n")

    def broken(path):
        raise PermissionError(path)
    monkeypatch.setattr(mm, "migrate_env_file", broken)
    startup._migrate_model(str(tmp_path))  # logs and returns
    assert dotenv_values(tmp_path / ".env")["EMBEDDING_MODEL"] == E5


def test_env_example_never_assigns_the_generation():
    # The installers merge uncommented env.example keys into an existing .env; a
    # merged marker would keep an old install on its old model.
    from pathlib import Path
    example = Path(__file__).resolve().parents[1] / "env.example"
    assert mm.read_env(str(example)) == (None, 1)


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlinked_env_keeps_its_link(tmp_path):
    target = write(tmp_path / "dotfiles.env", f"EMBEDDING_MODEL={E5}\n")
    link = tmp_path / ".env"
    link.symlink_to(target)
    mm.migrate_env_file(str(link), {})
    assert link.is_symlink()
    assert dotenv_values(target)["EMBEDDING_MODEL"] == mm.DEFAULT_MODEL


def test_cli_prints_the_effective_model_for_the_installers(tmp_path, capsys):
    env = write(tmp_path / ".env", f"EMBEDDING_MODEL=old/one\nexport 'EMBEDDING_MODEL' = \"{E5}\"\n")
    assert mm.main(["--print-model", str(env)]) == 0
    assert capsys.readouterr().out == f"{E5}\n"
    assert mm.main(["--print-model", str(tmp_path / "missing.env")]) == 0
    assert capsys.readouterr().out == "\n"
