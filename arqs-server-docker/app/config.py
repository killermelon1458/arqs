from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    app_name: str = os.getenv("ARQS_APP_NAME", "arqs-server")
    app_version: str = os.getenv("ARQS_APP_VERSION", "1.0.0-v1")
    host: str = os.getenv("ARQS_HOST", "0.0.0.0")
    port: int = int(os.getenv("ARQS_PORT", "8080"))
    database_path: str = os.getenv("ARQS_DATABASE_PATH", "/data/arqs.db")
    admin_api_key: str = os.getenv("ARQS_ADMIN_API_KEY", "change-me-admin-key")
    public_registration_enabled: bool = _env_bool("ARQS_PUBLIC_REGISTRATION_ENABLED", True)
    link_code_ttl_seconds: int = int(os.getenv("ARQS_LINK_CODE_TTL_SECONDS", "300"))
    adapter_provision_ttl_seconds: int = int(os.getenv("ARQS_ADAPTER_PROVISION_TTL_SECONDS", "600"))
    packet_id_ttl_seconds: int = int(os.getenv("ARQS_PACKET_ID_TTL_SECONDS", str(7 * 24 * 3600)))
    inbox_poll_interval_seconds: float = float(os.getenv("ARQS_INBOX_POLL_INTERVAL_SECONDS", "1.0"))
    log_level: str = os.getenv("ARQS_LOG_LEVEL", "INFO")
    enable_sql_trace: bool = _env_bool("ARQS_ENABLE_SQL_TRACE", False)
    register_rate_limit_window_seconds: int = int(os.getenv("ARQS_REGISTER_RATE_LIMIT_WINDOW_SECONDS", "3600"))
    register_rate_limit_max_attempts: int = int(os.getenv("ARQS_REGISTER_RATE_LIMIT_MAX_ATTEMPTS", "20"))
    trust_proxy_headers: bool = _env_bool("ARQS_TRUST_PROXY_HEADERS", False)
    trusted_proxy_cidrs: str = os.getenv("ARQS_TRUST_PROXY_CIDRS", "")
    trusted_proxy_ips: str = os.getenv("ARQS_TRUST_PROXY_IPS", "")

    @property
    def database_file(self) -> Path:
        return Path(self.database_path)


settings = Settings()
