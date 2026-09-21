"""Configuration loading.

Load order for the app config (later wins):

1. ``config/app.yaml``
2. ``config/local.yaml`` (optional, gitignored, per machine)
3. environment variables named ``CARQ__SECTION__KEY`` (nested with ``__``)

String values may reference the environment as ``${VAR}`` (required) or ``${VAR:-default}``.
A ``.env`` file in the repo root is loaded first, without overriding real environment variables.

Other config files added in later phases (``schema_contract.yaml``, ``llm.yaml``, ...) are
loaded with :func:`load_yaml`, which applies the same interpolation and pydantic validation.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

CONFIG_DIR_ENV = "CARQ_CONFIG_DIR"
ENV_OVERRIDE_PREFIX = "CARQ__"
APP_FILE = "app.yaml"
LOCAL_FILE = "local.yaml"

_INTERPOLATION = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
_SECRET_KEY_HINTS = ("secret", "password", "api_key", "apikey", "token", "connection_string")
REDACTED = "**********"

M = TypeVar("M", bound=BaseModel)


class ConfigError(Exception):
    """Raised when configuration cannot be found, parsed, interpolated or validated."""


# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Strict):
    data_root: Path
    catalog_path: Path
    docs_dir: Path = Path("docs")

    def resolved(self, root: Path) -> PathsConfig:
        """Return a copy with relative paths made absolute against ``root``."""
        return PathsConfig(
            data_root=_resolve(self.data_root, root),
            catalog_path=_resolve(self.catalog_path, root),
            docs_dir=_resolve(self.docs_dir, root),
        )


class LoggingConfig(_Strict):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    format: Literal["console", "json"] = "console"
    file: Path | None = None

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class StorageConfig(_Strict):
    """Where the parquet data is read from. Only ``local`` (``paths.data_root``) exists so far."""

    backend: Literal["local"] = "local"


class CatalogConfig(_Strict):
    """The persistent DuckDB catalog (the file itself is ``paths.catalog_path``)."""

    busy_timeout_seconds: float = Field(default=10.0, ge=0)  # retry while another process writes
    history_limit: int = Field(default=5, ge=1)  # refreshes listed by ``carq catalog status``


class AppConfig(_Strict):
    environment: str = "dev"
    paths: PathsConfig
    storage: StorageConfig = StorageConfig()
    catalog: CatalogConfig = CatalogConfig()
    logging: LoggingConfig = LoggingConfig()


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def find_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Locate the config directory.

    ``CARQ_CONFIG_DIR`` wins; otherwise search upwards from the current directory for
    ``config/app.yaml``; finally fall back to the source checkout this package lives in.
    """
    env = os.environ if env is None else env
    if env.get(CONFIG_DIR_ENV):
        path = Path(env[CONFIG_DIR_ENV]).expanduser().resolve()
        if not (path / APP_FILE).is_file():
            raise ConfigError(f"{CONFIG_DIR_ENV}={path} does not contain {APP_FILE}")
        return path

    for base in (Path.cwd(), *Path.cwd().parents, *Path(__file__).resolve().parents):
        candidate = base / "config"
        if (candidate / APP_FILE).is_file():
            return candidate
    raise ConfigError(f"Could not find config/{APP_FILE}; set {CONFIG_DIR_ENV}")


def load_config(
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load, merge, interpolate and validate the application config.

    ``env`` defaults to ``os.environ`` merged with the repo's ``.env`` file; pass an explicit
    mapping (as tests do) to make loading independent of the machine.
    """
    config_dir = find_config_dir(env) if config_dir is None else Path(config_dir).resolve()
    env = _default_env(config_dir) if env is None else env

    data = _read_yaml(config_dir / APP_FILE)
    local_path = config_dir / LOCAL_FILE
    if local_path.is_file():
        data = deep_merge(data, _read_yaml(local_path))
    data = deep_merge(data, env_overrides(env))
    data = interpolate(data, env, source=str(config_dir / APP_FILE))

    config = _validate(AppConfig, data, source=str(config_dir / APP_FILE))
    root = config_dir.parent
    log_file = _resolve(config.logging.file, root) if config.logging.file else None
    return config.model_copy(
        update={
            "paths": config.paths.resolved(root),
            "logging": config.logging.model_copy(update={"file": log_file}),
        }
    )


def load_yaml(
    name: str,
    model: type[M],
    config_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> M:
    """Load ``config/<name>`` into ``model`` with env interpolation (no local/env overrides)."""
    config_dir = find_config_dir(env) if config_dir is None else Path(config_dir).resolve()
    env = _default_env(config_dir) if env is None else env
    path = config_dir / name
    data = interpolate(_read_yaml(path), env, source=str(path))
    return _validate(model, data, source=str(path))


def redact(data: Any) -> Any:
    """Return a JSON-friendly copy of ``data`` with secret-looking values masked.

    ``SecretStr`` fields are always masked; plain strings are masked when their key looks
    like a secret (``api_key``, ``password``, ``connection_string``, ...).
    """
    if isinstance(data, BaseModel):
        data = data.model_dump(mode="json")
    if isinstance(data, SecretStr):
        return REDACTED
    if isinstance(data, dict):
        return {
            key: REDACTED
            if _looks_secret(key) and isinstance(value, str) and value
            else redact(value)
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [redact(item) for item in data]
    return data


# --------------------------------------------------------------------------------------
# Helpers (public for testing and reuse by later phases)
# --------------------------------------------------------------------------------------


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` into a copy of ``base``; non-dict values replace."""
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def env_overrides(env: Mapping[str, str]) -> dict[str, Any]:
    """Turn ``CARQ__A__B=value`` variables into ``{"a": {"b": value}}``.

    Values are parsed as YAML scalars, so ``true``, ``10`` and ``null`` get their natural types.
    """
    result: dict[str, Any] = {}
    for name, raw in env.items():
        if not name.upper().startswith(ENV_OVERRIDE_PREFIX):
            continue
        parts = [part.lower() for part in name[len(ENV_OVERRIDE_PREFIX) :].split("__") if part]
        if not parts:
            continue
        node = result
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ConfigError(f"Conflicting environment overrides at {name}")
        node[parts[-1]] = _parse_scalar(raw)
    return result


def interpolate(data: Any, env: Mapping[str, str], source: str = "<config>") -> Any:
    """Replace ``${VAR}`` / ``${VAR:-default}`` in all string values."""
    if isinstance(data, dict):
        return {key: interpolate(value, env, source) for key, value in data.items()}
    if isinstance(data, list):
        return [interpolate(item, env, source) for item in data]
    if not isinstance(data, str):
        return data

    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        value = env.get(name)
        if value not in (None, ""):
            return value
        if default is not None:
            return default
        raise ConfigError(f"{source}: environment variable {name} is required but not set")

    whole = _INTERPOLATION.fullmatch(data)
    if whole:  # a value that is only a reference keeps YAML typing (e.g. "${PORT:-8000}" -> 8000)
        return _parse_scalar(replace(whole))
    return _INTERPOLATION.sub(replace, data)


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _default_env(config_dir: Path) -> dict[str, str]:
    dotenv_path = config_dir.parent / ".env"
    file_values = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}
    merged = {key: value for key, value in file_values.items() if value is not None}
    merged.update(os.environ)  # real environment wins over .env
    return merged


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def _validate(model: type[M], data: Any, source: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(loc) for loc in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(f"{source}: invalid configuration\n" + "\n".join(lines)) from exc


def _parse_scalar(raw: str) -> Any:
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError:
        return raw
    return value if isinstance(value, str | int | float | bool) or value is None else raw


def _resolve(path: Path, root: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else (root / path).resolve()


def _looks_secret(key: Any) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_KEY_HINTS)
