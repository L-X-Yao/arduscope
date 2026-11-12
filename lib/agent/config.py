"""Local configuration for the online analysis agent.

The key is read from the provider's environment variable first, then from
this file. The file is written 0600; on Windows the mode is best effort.
Nothing here is ever written into a report.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from typing import Any

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".arduscope")
CONFIG_PATH = os.path.join(CONFIG_DIR, "agent.json")

PROVIDERS = ("anthropic", "openai")

# The provider's own variable, then one generic variable for services that
# speak the OpenAI API without being OpenAI.
_ENV_KEYS = {
    "anthropic": ("ANTHROPIC_API_KEY", "ARDUSCOPE_AI_KEY"),
    "openai": ("OPENAI_API_KEY", "ARDUSCOPE_AI_KEY"),
}


class ConfigError(ValueError):
    """The configuration file cannot be used."""


@dataclass
class AgentConfig:
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    allow_gps: bool = False
    allow_serial: bool = False
    allow_text: bool = False
    egress_ack: bool = False
    max_tool_calls: int = 20
    max_seconds: float = 300.0

    def resolved_key(self) -> str:
        """The key to use: environment first, then the file."""
        for name in _ENV_KEYS.get(self.provider, ()):
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return self.api_key.strip()

    def is_configured(self) -> bool:
        return bool(self.provider in PROVIDERS and self.model.strip()
                    and self.resolved_key())

    def public_dict(self) -> dict[str, Any]:
        """What the settings page may see: never the key itself."""
        key = self.resolved_key()
        data = asdict(self)
        data.pop("api_key", None)
        data["has_key"] = bool(key)
        data["key_hint"] = f"...{key[-4:]}" if len(key) >= 8 else ""
        data["config_path"] = CONFIG_PATH
        return data


def _from_mapping(data: dict) -> AgentConfig:
    known = {f for f in AgentConfig.__dataclass_fields__}
    clean = {k: v for k, v in data.items() if k in known}
    return AgentConfig(**clean)


def load_config(path: str | None = None) -> AgentConfig:
    """Read the file; a missing file is an empty configuration."""
    path = path or CONFIG_PATH
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return AgentConfig()
    except (OSError, ValueError) as e:
        raise ConfigError(str(e)) from e
    if not isinstance(data, dict):
        raise ConfigError("the file does not hold a JSON object")
    return _from_mapping(data)


def save_config(config: AgentConfig, path: str | None = None) -> None:
    """Write the file atomically, 0600, creating the directory 0700."""
    path = path or CONFIG_PATH
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".agent-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(asdict(config), f, indent=2, ensure_ascii=False)
            f.write("\n")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def config_from_args(provider: str, model: str, base_url: str = "",
                     api_key: str = "") -> AgentConfig:
    """Command-line values over the saved configuration.

    The key is never taken from argv; a saved key is reused, and the
    environment still wins inside resolved_key().
    """
    try:
        config = load_config()
    except ConfigError:
        config = AgentConfig()
    config.provider = provider or config.provider
    config.model = model or config.model
    config.base_url = base_url or config.base_url
    if api_key:
        config.api_key = api_key
    return config


def update_config(config: AgentConfig, changes: dict,
                  path: str | None = None) -> AgentConfig:
    """Apply changes from the settings form and save.

    An empty ``api_key`` keeps the stored key; ``clear_key`` removes it.
    """
    data = asdict(config)
    for key in ("provider", "base_url", "model", "allow_gps",
                "allow_serial", "allow_text", "egress_ack"):
        if key in changes:
            data[key] = changes[key]
    if changes.get("clear_key"):
        data["api_key"] = ""
    elif str(changes.get("api_key") or "").strip():
        data["api_key"] = str(changes["api_key"]).strip()
    updated = _from_mapping(data)
    save_config(updated, path)
    return updated
