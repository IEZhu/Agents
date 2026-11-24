"""The client profile switch (#734): interactive by default, headless on request."""

import logging

import src.engine.config as cfg


def test_profile_defaults_to_interactive_and_rejects_unknown_values(monkeypatch, caplog):
    monkeypatch.delenv("AGENTS_CLIENT_PROFILE", raising=False)
    choices = ("interactive", "headless")
    assert cfg._choice_env("AGENTS_CLIENT_PROFILE", "interactive", choices) == "interactive"
    monkeypatch.setenv("AGENTS_CLIENT_PROFILE", "Headless")
    assert cfg._choice_env("AGENTS_CLIENT_PROFILE", "interactive", choices) == "headless"
    monkeypatch.setenv("AGENTS_CLIENT_PROFILE", "benchmark")
    with caplog.at_level(logging.WARNING):
        assert cfg._choice_env("AGENTS_CLIENT_PROFILE", "interactive", choices) == "interactive"
    assert "AGENTS_CLIENT_PROFILE" in caplog.text


def test_headless_profile_reads_the_attribute_at_call_time(monkeypatch):
    monkeypatch.setattr(cfg, "CLIENT_PROFILE", "interactive")
    assert cfg.headless_profile() is False
    monkeypatch.setattr(cfg, "CLIENT_PROFILE", "headless")
    assert cfg.headless_profile() is True

