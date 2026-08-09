"""Type-safe configuration loaded from environment variables and .env files.

Every field carries a description for IDE hints and documentation.
Sensitive values use :class:`pydantic.SecretStr` to prevent log leaks.
"""

from __future__ import annotations

import os
from typing import Any

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Provider → (key_env, url_env, default_base_url) lookup priority.
# V5-compatible: discovers keys from system env vars by checking
# multiple well-known names in order.
_LLM_KEY_ENV_VARS: list[tuple[str, str, str, str]] = [
    ("LLM_API_KEY", "LLM_BASE_URL", "https://api.openai.com/v1", "text-embedding-3-small"),
    (
        "DASHSCOPE_API_KEY",
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "text-embedding-v4",
    ),
    (
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "https://api.deepseek.com/v1",
        "text-embedding-3-small",
    ),
]


class Settings(BaseSettings):
    """Application-wide settings with fail-fast validation on startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # -- Application ----------------------------------------------------------

    app_name: str = Field(
        default="erp-agent-copilot",
        description="Human-readable application name used in logs and traces.",
    )
    debug: bool = Field(
        default=False,
        description="Enable debug mode (more verbose logs, reload on change).",
    )

    # -- API Server -----------------------------------------------------------

    api_host: str = Field(
        default="0.0.0.0",
        description="Host address the API server binds to.",
    )
    api_port: int = Field(
        default=8000,
        description="Port the API server listens on.",
    )

    # -- Database -------------------------------------------------------------

    database_url: SecretStr = Field(
        description=(
            "PostgreSQL connection URL. Must include user, password, host, "
            "port, and database name. Example: postgresql://user:pass@localhost:5432/copilot"
        ),
    )

    # -- Redis ----------------------------------------------------------------

    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL for Celery broker and result backend.",
    )

    # -- LLM ------------------------------------------------------------------

    llm_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="API key for the LLM provider (OpenAI-compatible).",
    )
    llm_base_url: str = Field(
        default="https://api.openai.com/v1",
        description="Base URL for the LLM API endpoint.",
    )
    llm_model: str = Field(
        default="gpt-4o",
        description="Default LLM model name to use for agent reasoning.",
    )
    embedding_model: str = Field(
        default="text-embedding-3-small",
        description="Embedding model name for semantic retrieval.",
    )

    @model_validator(mode="before")
    @classmethod
    def _discover_llm_key(cls, data: Any) -> Any:
        """Discover LLM credentials from system env vars (V5-compatible).

        When llm_api_key is empty, checks well-known env var names
        (DEEPSEEK_API_KEY, DASHSCOPE_API_KEY) and fills in
        llm_api_key / llm_base_url from the first match.
        """
        if isinstance(data, dict):
            existing_key = data.get("llm_api_key", "")
        else:
            existing_key = getattr(data, "llm_api_key", "") if data else ""

        if existing_key:
            return data

        for key_env, url_env, default_url, default_model in _LLM_KEY_ENV_VARS:
            key = os.getenv(key_env, "")
            if key:
                url = os.getenv(url_env, "") or default_url
                if isinstance(data, dict):
                    data["llm_api_key"] = key
                    data["llm_base_url"] = url
                    data.setdefault("embedding_model", default_model)
                return data

        return data

    # -- Celery ---------------------------------------------------------------

    celery_concurrency: int = Field(
        default=4,
        ge=1,
        description="Number of concurrent Celery worker processes.",
    )

    # -- Observability --------------------------------------------------------

    otel_exporter_endpoint: str = Field(
        default="http://localhost:4317",
        description="OpenTelemetry collector gRPC endpoint.",
    )
    log_level: str = Field(
        default="INFO",
        description="Minimum log level for the root logger.",
    )
