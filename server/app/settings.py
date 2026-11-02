from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PROCURE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    environment: Literal["development", "production", "test"] = "development"
    instance_id: str | None = Field(default=None, min_length=1, max_length=128)
    database_url: SecretStr | None = None
    files_dir: Path = Path(".local/files")
    maintenance_dir: Path | None = None
    session_hours: int = Field(default=12, ge=1, le=720)
    # 可调整的技术默认值，后续解析资源实验可按环境修改，不作为业务额度。
    max_file_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=1024 * 1024 * 1024)
    command_lock_timeout_ms: int = Field(default=3000, ge=1, le=60000)

    @field_validator("instance_id")
    @classmethod
    def stable_instance_id(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or value != value.strip()):
            raise ValueError("Instance ID must be a nonempty stable configured value")
        return value

    @field_validator("database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("postgresql://", "postgresql+psycopg://", "postgres://")
        ):
            raise ValueError("Only PostgreSQL database URLs are supported")
        return value
