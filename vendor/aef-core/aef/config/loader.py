"""Loads and validates `AgentConfig` from a YAML file. Fails loudly: a
missing file, invalid YAML, or a pydantic validation error (including
unknown keys) all raise `AgentConfigError` with a readable, field-by-field
message rather than propagating a raw traceback.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from aef.config.schema import AgentConfig


class AgentConfigError(ValueError):
    pass


def _format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"invalid agent config at {source}:"]
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"]) or "<root>"
        lines.append(f"  - {location}: {error['msg']}")
    return "\n".join(lines)


def load_agent_config_text(raw_text: str, *, source: str) -> AgentConfig:
    """Validate config that is already in hand, from wherever it came.

    Split out from `load_agent_config` so the gate can validate the config as
    of the BASE REF — `git show <base>:aef.yaml` returns text, not a path,
    and reading the path would read the candidate's version of the rules it
    is being judged by (ADR 0082).
    """
    try:
        raw = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise AgentConfigError(f"invalid YAML in agent config at {source}: {exc}") from exc

    if not isinstance(raw, dict):
        raise AgentConfigError(
            f"agent config at {source} must be a YAML mapping, got {type(raw).__name__}"
        )

    try:
        return AgentConfig.model_validate(raw)
    except ValidationError as exc:
        raise AgentConfigError(_format_validation_error(exc, source)) from exc


def load_agent_config(path: str | Path) -> AgentConfig:
    path = Path(path)
    try:
        raw_text = path.read_text()
    except UnicodeDecodeError as exc:
        raise AgentConfigError(f"cannot decode agent config at {path} as UTF-8: {exc}") from exc
    except OSError as exc:
        raise AgentConfigError(f"cannot read agent config at {path}: {exc}") from exc
    return load_agent_config_text(raw_text, source=str(path))
