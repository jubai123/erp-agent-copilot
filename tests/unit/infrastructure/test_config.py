"""Tests for Pydantic Settings configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from erp_copilot.infrastructure.config import Settings


class TestSettingsDefaults:
    """Every config item should have a sensible default or be explicitly required."""

    def test_app_settings_have_defaults(self) -> None:
        settings = Settings(database_url="postgresql://localhost/test", llm_api_key="sk-test")
        assert settings.app_name == "erp-agent-copilot"
        assert settings.debug is False

    def test_api_settings_have_defaults(self) -> None:
        settings = Settings(database_url="postgresql://localhost/test", llm_api_key="sk-test")
        assert settings.api_host == "0.0.0.0"
        assert settings.api_port == 8000

    def test_database_url_has_no_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """database_url must be provided explicitly (fail-fast)."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", ".env.nonexistent")
        with pytest.raises(ValidationError):
            Settings(llm_api_key="sk-test")  # type: ignore[call-arg]

    def test_llm_api_key_optional_with_discovery(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """llm_api_key is optional — discovery fills from DEEPSEEK/DASHSCOPE env vars."""
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", ".env.nonexistent")
        # Should not raise — llm_api_key defaults to empty when no key is available
        settings = Settings(database_url="postgresql://localhost/test")
        assert settings.llm_api_key.get_secret_value() == ""

    def test_redis_url_has_default(self) -> None:
        settings = Settings(database_url="postgresql://localhost/test", llm_api_key="sk-test")
        assert settings.redis_url == "redis://localhost:6379/0"

    def test_llm_base_url_has_default(self) -> None:
        settings = Settings(database_url="postgresql://localhost/test", llm_api_key="sk-test")
        assert settings.llm_base_url == "https://api.openai.com/v1"

    def test_celery_concurrency_has_default(self) -> None:
        settings = Settings(database_url="postgresql://localhost/test", llm_api_key="sk-test")
        assert settings.celery_concurrency == 4


class TestSharedEnvTolerance:
    """The root .env is shared with docker-compose interpolation (e.g.
    GRAFANA_ADMIN_PASSWORD, which the app never consumes). Settings must
    tolerate compose-only keys instead of failing boot with extra_forbidden."""

    def test_compose_only_env_var_does_not_block_boot(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text(
            "DATABASE_URL=postgresql://localhost/compose-shared-test\n"
            "LLM_API_KEY=sk-test\n"
            "GRAFANA_ADMIN_PASSWORD=supersecret\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("LLM_API_KEY", raising=False)
        monkeypatch.setitem(Settings.model_config, "env_file", str(env_file))
        settings = Settings()
        assert (
            settings.database_url.get_secret_value() == "postgresql://localhost/compose-shared-test"
        )
        assert settings.llm_api_key.get_secret_value() == "sk-test"


class TestSecretStr:
    """Sensitive values must use SecretStr so they are never logged accidentally."""

    def test_database_url_is_secret(self) -> None:
        settings = Settings(
            database_url="postgresql://user:pass@localhost/db",
            llm_api_key="sk-test",
        )
        assert isinstance(settings.database_url, SecretStr)

    def test_llm_api_key_is_secret(self) -> None:
        settings = Settings(
            database_url="postgresql://localhost/test",
            llm_api_key="sk-secret-key",
        )
        assert isinstance(settings.llm_api_key, SecretStr)

    def test_secret_str_masked_in_repr(self) -> None:
        settings = Settings(
            database_url="postgresql://user:pass@localhost/db",
            llm_api_key="sk-secret-key",
        )
        repr_str = repr(settings)
        assert "pass" not in repr_str
        assert "sk-secret-key" not in repr_str

    def test_secret_str_masked_in_str(self) -> None:
        settings = Settings(
            database_url="postgresql://user:pass@localhost/db",
            llm_api_key="sk-secret-key",
        )
        str_val = str(settings)
        assert "pass" not in str_val
        assert "sk-secret-key" not in str_val

    def test_secret_value_retrievable(self) -> None:
        settings = Settings(
            database_url="postgresql://user:pass@localhost/db",
            llm_api_key="sk-secret-key",
        )
        secret = settings.llm_api_key.get_secret_value()
        assert secret == "sk-secret-key"


class TestApiKeyPepperFailClosed:
    """api_key mode must never boot with an empty pepper (fail-closed)."""

    def test_api_key_mode_requires_pepper(self) -> None:
        # Empty pepper would HMAC key hashes with an empty secret — refuse to
        # boot so a misconfigured production never runs in a weakened state.
        with pytest.raises(ValidationError):
            Settings(
                database_url="postgresql://localhost/test",
                auth_mode="api_key",
                api_key_pepper="",
            )

    def test_api_key_mode_accepts_pepper(self) -> None:
        settings = Settings(
            database_url="postgresql://localhost/test",
            auth_mode="api_key",
            api_key_pepper="s3cr3t-pepper",
        )
        assert settings.api_key_pepper == "s3cr3t-pepper"

    def test_api_key_mode_rejects_whitespace_only_pepper(self) -> None:
        # A whitespace-only pepper is effectively empty (HMAC keyed with "   ").
        with pytest.raises(ValidationError):
            Settings(
                database_url="postgresql://localhost/test",
                auth_mode="api_key",
                api_key_pepper="   ",
            )

    def test_header_mode_allows_empty_pepper(self) -> None:
        # Development header mode trusts headers directly and hashes no keys,
        # so no pepper is needed there.
        settings = Settings(
            database_url="postgresql://localhost/test",
            auth_mode="header",
            api_key_pepper="",
        )
        assert settings.auth_mode == "header"


class TestEnvLoading:
    """Settings should load from environment variables."""

    def test_loads_from_env_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://env:var@localhost/db")
        monkeypatch.setenv("LLM_API_KEY", "sk-from-env")
        monkeypatch.setenv("APP_NAME", "env-copilot")
        monkeypatch.setenv("API_PORT", "9000")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.database_url.get_secret_value() == "postgresql://env:var@localhost/db"
        assert settings.llm_api_key.get_secret_value() == "sk-from-env"
        assert settings.app_name == "env-copilot"
        assert settings.api_port == 9000

    def test_env_var_overrides_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("REDIS_URL", "redis://custom:6379/1")

        settings = Settings()  # type: ignore[call-arg]

        assert settings.redis_url == "redis://custom:6379/1"

    def test_invalid_port_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("API_PORT", "not-a-number")

        with pytest.raises(ValidationError):
            Settings()


class TestDescriptions:
    """Every config field must have a description for documentation."""

    def test_all_fields_have_descriptions(self) -> None:
        for name, field_info in Settings.model_fields.items():
            assert field_info.description, f"Field '{name}' is missing a description"
