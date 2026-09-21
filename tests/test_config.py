from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from carquery.config import (
    REDACTED,
    AppConfig,
    ConfigError,
    env_overrides,
    find_config_dir,
    interpolate,
    load_config,
    load_yaml,
    redact,
)


def test_repo_config_loads_with_defaults(repo_config_dir: Path) -> None:
    config = load_config(repo_config_dir, env={})

    assert isinstance(config, AppConfig)
    assert config.environment == "dev"
    assert config.logging.level == "INFO"
    assert config.paths.data_root == (repo_config_dir.parent / "data").resolve()
    assert config.paths.catalog_path.name == "catalog.duckdb"


def test_relative_paths_resolve_against_repo_root(config_dir: Path) -> None:
    config = load_config(config_dir, env={})
    assert config.paths.data_root == (config_dir.parent / "data").resolve()


def test_absolute_paths_are_kept(config_dir: Path, tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    config = load_config(config_dir, env={"DATA_ROOT": str(target)})
    assert config.paths.data_root == target


def test_precedence_yaml_then_local_then_env(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("logging:\n  level: WARNING\n", encoding="utf-8")

    assert load_config(config_dir, env={}).logging.level == "WARNING"
    with_env = load_config(config_dir, env={"CARQ__LOGGING__LEVEL": "error"})
    assert with_env.logging.level == "ERROR"


def test_local_override_keeps_sibling_keys(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("logging:\n  format: json\n", encoding="utf-8")
    config = load_config(config_dir, env={})
    assert config.logging.format == "json"
    assert config.logging.level == "INFO"


def test_interpolation_uses_env_value_and_default(config_dir: Path) -> None:
    assert load_config(config_dir, env={}).environment == "dev"
    assert load_config(config_dir, env={"CARQ_ENV": "prod"}).environment == "prod"


def test_missing_required_env_var_names_it(config_dir: Path) -> None:
    (config_dir / "app.yaml").write_text(
        "paths:\n  data_root: ${MUST_BE_SET}\n  catalog_path: x.duckdb\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="MUST_BE_SET"):
        load_config(config_dir, env={})


def test_interpolation_inside_longer_string_and_typed_whole_values() -> None:
    env = {"HOST": "example.org", "PORT": "8443"}
    result = interpolate({"url": "https://${HOST}:${PORT}/api", "port": "${PORT}"}, env)
    assert result == {"url": "https://example.org:8443/api", "port": 8443}


def test_env_overrides_build_nested_typed_values() -> None:
    overrides = env_overrides(
        {
            "CARQ__LOGGING__LEVEL": "DEBUG",
            "CARQ__A__B__C": "true",
            "CARQ__LIMIT": "10",
            "OTHER": "ignored",
        }
    )
    assert overrides == {"logging": {"level": "DEBUG"}, "a": {"b": {"c": True}}, "limit": 10}


def test_unknown_keys_are_rejected(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match="loging"):
        load_config(config_dir, env={"CARQ__LOGING__LEVEL": "DEBUG"})


def test_invalid_value_gives_clear_error(config_dir: Path) -> None:
    with pytest.raises(ConfigError, match=r"logging\.level"):
        load_config(config_dir, env={"CARQ__LOGGING__LEVEL": "LOUD"})


def test_invalid_yaml_is_reported(config_dir: Path) -> None:
    (config_dir / "local.yaml").write_text("logging: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_config(config_dir, env={})


def test_find_config_dir_uses_env_var(config_dir: Path) -> None:
    assert find_config_dir({"CARQ_CONFIG_DIR": str(config_dir)}) == config_dir.resolve()


def test_find_config_dir_rejects_bad_env_var(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="CARQ_CONFIG_DIR"):
        find_config_dir({"CARQ_CONFIG_DIR": str(tmp_path)})


class _ProviderConfig(BaseModel):
    base_url: str
    api_key: SecretStr


def test_load_yaml_generic_model_with_secret(config_dir: Path) -> None:
    (config_dir / "llm.yaml").write_text(
        "base_url: https://openrouter.ai/api/v1\napi_key: ${OPENROUTER_API_KEY}\n",
        encoding="utf-8",
    )
    loaded = load_yaml("llm.yaml", _ProviderConfig, config_dir, env={"OPENROUTER_API_KEY": "sk-1"})

    assert loaded.api_key.get_secret_value() == "sk-1"
    assert "sk-1" not in repr(loaded)


def test_redact_masks_secretstr_and_secret_looking_keys() -> None:
    model = _ProviderConfig(base_url="https://x", api_key=SecretStr("sk-1"))
    assert redact(model) == {"base_url": "https://x", "api_key": REDACTED}

    plain = {
        "storage": {"connection_string": "DefaultEndpoints...", "account": "acct"},
        "items": [{"password": "p"}],
        "empty_token": "",
    }
    assert redact(plain) == {
        "storage": {"connection_string": REDACTED, "account": "acct"},
        "items": [{"password": REDACTED}],
        "empty_token": "",
    }
