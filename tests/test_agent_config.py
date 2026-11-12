"""Agent configuration: where the key comes from and what is shown."""

from __future__ import annotations

import json
import os
import stat

import pytest

from lib.agent.config import (AgentConfig, ConfigError, load_config,
                              save_config, update_config)

pytestmark = pytest.mark.fast


def test_missing_file_is_an_empty_config(tmp_path):
    config = load_config(str(tmp_path / "missing.json"))
    assert config.provider == ""
    assert not config.is_configured()


def test_environment_key_wins(tmp_path, monkeypatch):
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="m",
                            api_key="from-file"), str(path))
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    config = load_config(str(path))
    assert config.resolved_key() == "from-env"


def test_file_is_written_0600(tmp_path):
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="m",
                            api_key="secret"), str(path))
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["api_key"] == "secret"


def test_public_dict_never_carries_the_key(tmp_path):
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="m",
                            api_key="sk-secret-1234"), str(path))
    public = load_config(str(path)).public_dict()
    assert "api_key" not in public
    assert public["has_key"] is True
    assert public["key_hint"] == "...1234"


def test_empty_key_keeps_the_stored_one(tmp_path):
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="m",
                            api_key="keep-me"), str(path))
    updated = update_config(load_config(str(path)), {"api_key": "",
                                                     "model": "other"},
                            str(path))
    assert updated.api_key == "keep-me"
    assert updated.model == "other"


def test_clear_key_removes_it(tmp_path):
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="m",
                            api_key="remove-me"), str(path))
    updated = update_config(load_config(str(path)), {"clear_key": True},
                            str(path))
    assert updated.api_key == ""


def test_a_broken_file_is_an_error(tmp_path):
    path = tmp_path / "agent.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_config(str(path))


def test_command_line_values_reuse_the_saved_key(tmp_path, monkeypatch):
    import lib.agent.config as agent_config
    path = tmp_path / "agent.json"
    save_config(AgentConfig(provider="openai", model="saved-model",
                            api_key="saved-key"), str(path))
    monkeypatch.setattr(agent_config, "CONFIG_PATH", str(path))
    config = agent_config.config_from_args("openai", "cli-model")
    assert config.api_key == "saved-key"
    assert config.model == "cli-model"
