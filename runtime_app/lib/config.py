from __future__ import annotations

from functools import lru_cache

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    backend_internal_base_url: str = Field(
        default="http://backend:8000/api/v1/internal/runtime",
        validation_alias="BACKEND_INTERNAL_BASE_URL",
    )
    stell_ai_base_url: str = Field(
        default="http://stellai:7020",
        validation_alias="STELL_AI_BASE_URL",
    )
    internal_service_token: str = Field(
        ...,
        validation_alias=AliasChoices("INTERNAL_SERVICE_TOKEN", "BOOTSTRAP_ADMIN_TOKEN"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
