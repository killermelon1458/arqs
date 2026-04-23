from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    app_name: str = "ARQS Server"
    api_key_header: str = "X-ARQS-API-Key"


@dataclass(slots=True)
class StorageConfig:
    db_path: str = "/data/arqs.db"
    sqlite_wal: bool = True
    sqlite_busy_timeout_ms: int = 5000


@dataclass(slots=True)
class RetentionConfig:
    default_packet_ttl_seconds: int = 30 * 24 * 60 * 60
    no_expiry: bool = False
    link_code_ttl_seconds: int = 10 * 60


@dataclass(slots=True)
class LimitsConfig:
    max_storage_bytes: int = 40 * 1024 * 1024 * 1024
    max_packet_bytes: int = 256 * 1024
    max_queued_packets_per_endpoint: int = 10_000
    max_queued_bytes_per_endpoint: int = 1 * 1024 * 1024 * 1024
    max_queued_bytes_per_node: int = 5 * 1024 * 1024 * 1024
    max_total_queued_packets: int = 100_000
    max_total_queued_bytes: int = 40 * 1024 * 1024 * 1024
    max_inbox_batch: int = 200
    long_poll_max_seconds: int = 30


@dataclass(slots=True)
class RateLimitConfig:
    send_window_seconds: int = 60
    max_sends_per_window: int = 600


@dataclass(slots=True)
class NetworkConfig:
    trusted_proxies: list[str] = field(default_factory=list)
    trusted_forwarded_headers: list[str] = field(default_factory=lambda: ["x-forwarded-for", "x-real-ip"])
    ip_access_mode: str = "dynamic"


@dataclass(slots=True)
class MaintenanceConfig:
    cleanup_interval_seconds: int = 60


@dataclass(slots=True)
class ObservabilityConfig:
    health_mode: str = "public"
    stats_mode: str = "public"


@dataclass(slots=True)
class BlacklistConfig:
    client_ips: list[str] = field(default_factory=list)
    node_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    blacklist: BlacklistConfig = field(default_factory=BlacklistConfig)


def _overlay_dataclass(instance, values: dict) -> None:
    for key, value in values.items():
        if hasattr(instance, key):
            setattr(instance, key, value)


_OBSERVABILITY_MODES = {"public", "node_api_key", "off"}


def _validate_observability_config(config: ObservabilityConfig) -> None:
    for field_name in ("health_mode", "stats_mode"):
        value = getattr(config, field_name)
        if value not in _OBSERVABILITY_MODES:
            allowed = ", ".join(sorted(_OBSERVABILITY_MODES))
            raise ValueError(f"unsupported observability.{field_name}: {value!r}; expected one of: {allowed}")


def load_config() -> AppConfig:
    config = AppConfig()
    path = Path(os.environ.get("ARQS_CONFIG", "/app/config.toml"))
    if path.exists():
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
        if isinstance(raw.get("server"), dict):
            _overlay_dataclass(config.server, raw["server"])
        if isinstance(raw.get("storage"), dict):
            _overlay_dataclass(config.storage, raw["storage"])
        if isinstance(raw.get("retention"), dict):
            _overlay_dataclass(config.retention, raw["retention"])
        if isinstance(raw.get("limits"), dict):
            _overlay_dataclass(config.limits, raw["limits"])
        if isinstance(raw.get("rate_limit"), dict):
            _overlay_dataclass(config.rate_limit, raw["rate_limit"])
        if isinstance(raw.get("network"), dict):
            _overlay_dataclass(config.network, raw["network"])
        if isinstance(raw.get("maintenance"), dict):
            _overlay_dataclass(config.maintenance, raw["maintenance"])
        if isinstance(raw.get("observability"), dict):
            _overlay_dataclass(config.observability, raw["observability"])
        if isinstance(raw.get("blacklist"), dict):
            _overlay_dataclass(config.blacklist, raw["blacklist"])
    _validate_observability_config(config.observability)
    return config
