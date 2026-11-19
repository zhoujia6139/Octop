"""Configuration for the independent cloud and local applications."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


def application_home(kind: Literal["cloud", "local"]) -> Path:
    root = Path(os.environ.get("OCTOP_HOME") or Path.home() / ".octop").expanduser()
    return root / kind


class ApplicationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    home: Path
    bind_host: str = "127.0.0.1"
    port: int = Field(default=8088, ge=0, le=65535)
    default_timezone: str = "Asia/Shanghai"
    enable_api_docs: bool = False

    @field_validator("home")
    @classmethod
    def normalized_home(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @field_validator("default_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @classmethod
    def load(cls, home: Path) -> Self:
        path = home / "config.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("application config must be a JSON object")
        data["home"] = home
        for env_key, field in (
            ("OCTOP_BIND_HOST", "bind_host"),
            ("OCTOP_PORT", "port"),
            ("OCTOP_DEFAULT_TIMEZONE", "default_timezone"),
        ):
            if os.environ.get(env_key):
                data[field] = os.environ[env_key]
        return cls.model_validate(data)


class CloudSettings(ApplicationSettings):
    database_url: SecretStr | None = None

    @field_validator("database_url")
    @classmethod
    def postgres_url(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not value.get_secret_value().startswith(
            ("postgresql://", "postgres://")
        ):
            raise ValueError("database_url must be a PostgreSQL URL")
        return value

    @classmethod
    def load(cls, home: Path) -> Self:
        settings = super().load(home)
        if value := os.environ.get("OCTOP_CLOUD_DATABASE_URL"):
            return cls.model_validate({**settings.model_dump(), "database_url": value})
        return settings


class LocalSettings(ApplicationSettings):
    cloud_url: str | None = None

    @field_validator("cloud_url")
    @classmethod
    def trusted_cloud_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("cloud_url must be an origin without credentials or a path")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise ValueError(
                "cloud_url requires HTTPS (HTTP is allowed only on loopback for development)"
            )
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        port = parsed.port
        suffix = (
            f":{port}"
            if port is not None and port != (443 if parsed.scheme == "https" else 80)
            else ""
        )
        return f"{parsed.scheme}://{host}{suffix}"

    port: int = Field(default=0, ge=0, le=65535)
    api_token: SecretStr = Field(default_factory=lambda: SecretStr(secrets.token_urlsafe(32)))

    @field_validator("bind_host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("the local application must bind to loopback")
        return value

    @field_validator("api_token")
    @classmethod
    def strong_token(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("local API token must contain at least 32 characters")
        return value

    @classmethod
    def load(cls, home: Path) -> Self:
        settings = super().load(home)
        if cloud_url := os.environ.get("OCTOP_CLOUD_URL"):
            settings = cls.model_validate({**settings.model_dump(), "cloud_url": cloud_url})
        if value := os.environ.get("OCTOP_LOCAL_API_TOKEN"):
            return cls.model_validate({**settings.model_dump(), "api_token": value})
        return settings
