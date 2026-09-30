"""Configuration: config.yaml models and resolution of where graph-me keeps its data.

graph-me has a *base directory* holding ``config.yaml`` and, by default, ``graph-out/``:

- inside a clone of the graph-me repo: the clone root;
- otherwise: ``~/graph-me``.

The output folder (``graph-out``) is resolved in this order, first match wins:

1. ``--out`` flag or ``GRAPH_ME_OUT`` environment variable
2. ``output:`` in ``config.yaml``
3. ``<base>/graph-out``
"""

from __future__ import annotations

import os
import tomllib
from importlib import resources
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_OUT = "GRAPH_ME_OUT"
ENV_CONFIG = "GRAPH_ME_CONFIG"
CONFIG_NAME = "config.yaml"
TEMPLATE_NAME = "config-template.yaml"
OUT_NAME = "graph-out"
DB_NAME = "graph.db"
HOME_BASE = Path("~/graph-me")
PROJECT_NAME = "graph-me"


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["agent", "ollama", "anthropic", "openai_compat", "fake"] = "agent"
    model: str | None = None
    base_url: str | None = None


class TierConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    llm: LLMConfig | None = None
    embeddings: str | None = None
    ner: str | None = None


class ExtractionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_tier: Literal["none", "medium", "high"] = "none"
    medium: TierConfig = Field(default_factory=lambda: TierConfig(llm=LLMConfig()))
    high: TierConfig = Field(default_factory=TierConfig)


class SourceConfig(BaseModel):
    """One entry under ``sources:``. Connector-specific keys are kept as extra fields."""

    model_config = ConfigDict(extra="allow")

    type: str


class BlacklistConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paths: list[str] = Field(default_factory=list)
    contacts: list[str] = Field(default_factory=list)
    patterns: list[str] = Field(default_factory=list)


class PeopleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Country calling code for national phone numbers ("06 12 34 56 78" -> +33612345678),
    # so a contact's number matches the same person on WhatsApp. Example: "33".
    phone_country_code: str | None = None
    # Your own emails and phone numbers, when a source can't tell which messages are yours.
    me: list[str] = Field(default_factory=list)
    # Time zone used to tell which day a message was sent (birthday wishes just after
    # midnight). IANA name such as "Europe/Paris". Default: this computer's time zone.
    timezone: str | None = None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError(f"unknown time zone {value!r} (example: Europe/Paris)") from exc
        return value


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output: Path | None = None
    people: PeopleConfig = Field(default_factory=PeopleConfig)
    extraction: ExtractionConfig = Field(default_factory=ExtractionConfig)
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    blacklist: BlacklistConfig = Field(default_factory=BlacklistConfig)


def find_clone_root(start: Path | None = None) -> Path | None:
    """Return the root of a graph-me source checkout containing ``start``, if any."""
    here = (start or Path.cwd()).resolve()
    for folder in (here, *here.parents):
        pyproject = folder / "pyproject.toml"
        if not pyproject.is_file():
            continue
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if data.get("project", {}).get("name") == PROJECT_NAME:
            return folder
    return None


def base_dir(cwd: Path | None = None) -> Path:
    clone = find_clone_root(cwd)
    return clone if clone else HOME_BASE.expanduser()


def config_path(explicit: Path | None = None, cwd: Path | None = None) -> Path:
    if explicit:
        return explicit.expanduser()
    if env := os.environ.get(ENV_CONFIG):
        return Path(env).expanduser()
    return base_dir(cwd) / CONFIG_NAME


def template_text() -> str:
    """The commented config template copied by ``graph-me init``.

    Installed wheels carry it inside the package; a source checkout reads it from the repo root.
    """
    packaged = resources.files("graph_me").joinpath(TEMPLATE_NAME)
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    return (Path(__file__).resolve().parents[2] / TEMPLATE_NAME).read_text(encoding="utf-8")


def load_config(path: Path) -> Config:
    """Load and validate ``config.yaml``. A missing file means all defaults."""
    if not path.is_file():
        return Config()
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return Config.model_validate(raw)


def resolve_output(
    cli_out: Path | None = None,
    config: Config | None = None,
    cwd: Path | None = None,
) -> Path:
    """Where ``graph-out`` lives. See the module docstring for the order."""
    if cli_out:
        return cli_out.expanduser().resolve()
    if env := os.environ.get(ENV_OUT):
        return Path(env).expanduser().resolve()
    if config and config.output:
        return config.output.expanduser().resolve()
    return (base_dir(cwd) / OUT_NAME).resolve()


def ensure_output(out: Path) -> Path:
    """Create the output folder, private to the user and ignored by git wherever it lives."""
    out.mkdir(parents=True, exist_ok=True)
    out.chmod(0o700)
    # A self-ignoring .gitignore keeps personal data out of any repo the folder ends up in,
    # without editing the user's own .gitignore.
    ignore = out / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# graph-me: personal data, never commit\n*\n", encoding="utf-8")
    return out
