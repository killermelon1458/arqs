from __future__ import annotations

import ipaddress
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import SessionLocal, get_config


RUNTIME_SETTINGS_ID = 1
_MARKER_FILENAME = "runtime_access_cache.marker"
_WATCH_INTERVAL_SECONDS = 0.5
_DEFAULT_IP_POLICY = "allow"


@dataclass(frozen=True)
class RuntimeAccessSnapshot:
    runtime_settings: dict[str, Any]
    ip_access_mode: str
    default_ip_policy: str
    allowed_ips: frozenset[str]
    denied_ips: frozenset[str]
    loaded_at: datetime


_logger = logging.getLogger("arqs.app")
_cache_lock = threading.Lock()
_snapshot: RuntimeAccessSnapshot | None = None
_last_marker_token: str = ""
_watcher_thread: threading.Thread | None = None
_watcher_stop_event = threading.Event()


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _normalize_ip(ip: str) -> str | None:
    raw = str(ip).strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _static_config_ip_denied(cfg: AppConfig, normalized_ip: str | None) -> bool:
    if normalized_ip is None:
        return False
    for candidate in cfg.blacklist.client_ips:
        if _normalize_ip(candidate) == normalized_ip:
            return True
    return False


def _runtime_settings_defaults(cfg: AppConfig) -> dict[str, Any]:
    return {
        "settings_id": RUNTIME_SETTINGS_ID,
        "max_storage_bytes": int(cfg.limits.max_storage_bytes),
        "max_packet_bytes": int(cfg.limits.max_packet_bytes),
        "max_queued_packets_per_endpoint": int(cfg.limits.max_queued_packets_per_endpoint),
        "max_queued_bytes_per_endpoint": int(cfg.limits.max_queued_bytes_per_endpoint),
        "max_queued_bytes_per_node": int(cfg.limits.max_queued_bytes_per_node),
        "max_total_queued_packets": int(cfg.limits.max_total_queued_packets),
        "max_total_queued_bytes": int(cfg.limits.max_total_queued_bytes),
        "max_inbox_batch": int(cfg.limits.max_inbox_batch),
        "long_poll_max_seconds": int(cfg.limits.long_poll_max_seconds),
        "send_window_seconds": int(cfg.rate_limit.send_window_seconds),
        "max_sends_per_window": int(cfg.rate_limit.max_sends_per_window),
        "default_ip_policy": _DEFAULT_IP_POLICY,
        "updated_at": _utcnow(),
    }


def _marker_path(cfg: AppConfig) -> Path:
    db_path = Path(cfg.storage.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path.parent / _MARKER_FILENAME


def _read_marker_token(cfg: AppConfig) -> str:
    path = _marker_path(cfg)
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def touch_runtime_access_marker(cfg: AppConfig | None = None) -> None:
    cfg = cfg or get_config()
    path = _marker_path(cfg)
    path.write_text(f"{time.time_ns()}\n", encoding="utf-8")


def mark_runtime_access_dirty(db: Session) -> None:
    db.info["runtime_access_cache_dirty"] = True


def _load_snapshot_from_db(cfg: AppConfig) -> RuntimeAccessSnapshot:
    defaults = _runtime_settings_defaults(cfg)
    mode = str(cfg.network.ip_access_mode).strip().lower()
    allowed_ips: set[str] = set()
    denied_ips: set[str] = set()

    with SessionLocal() as db:
        row = db.execute(
            text(
                """
                SELECT
                    settings_id,
                    max_storage_bytes,
                    max_packet_bytes,
                    max_queued_packets_per_endpoint,
                    max_queued_bytes_per_endpoint,
                    max_queued_bytes_per_node,
                    max_total_queued_packets,
                    max_total_queued_bytes,
                    max_inbox_batch,
                    long_poll_max_seconds,
                    send_window_seconds,
                    max_sends_per_window,
                    default_ip_policy,
                    updated_at
                FROM runtime_settings
                WHERE settings_id = :settings_id
                """
            ),
            {"settings_id": RUNTIME_SETTINGS_ID},
        ).mappings().first()

        runtime_settings = defaults
        if row is not None:
            runtime_settings = {**defaults, **dict(row)}

        if mode == "dynamic":
            rows = db.execute(
                text(
                    """
                    SELECT ip, action
                    FROM ip_access_rules
                    ORDER BY ip ASC
                    """
                )
            ).mappings().all()
            for entry in rows:
                normalized_ip = _normalize_ip(str(entry["ip"]))
                if normalized_ip is None:
                    continue
                if str(entry["action"]).strip().lower() == "allow":
                    allowed_ips.add(normalized_ip)
                    denied_ips.discard(normalized_ip)
                else:
                    denied_ips.add(normalized_ip)
                    allowed_ips.discard(normalized_ip)

    return RuntimeAccessSnapshot(
        runtime_settings=runtime_settings,
        ip_access_mode=mode,
        default_ip_policy=str(runtime_settings["default_ip_policy"]).strip().lower(),
        allowed_ips=frozenset(allowed_ips),
        denied_ips=frozenset(denied_ips),
        loaded_at=_utcnow(),
    )


def _store_snapshot(snapshot: RuntimeAccessSnapshot, marker_token: str) -> None:
    global _snapshot, _last_marker_token
    with _cache_lock:
        _snapshot = snapshot
        _last_marker_token = marker_token


def initialize_runtime_access_cache(cfg: AppConfig | None = None) -> None:
    cfg = cfg or get_config()
    snapshot = _load_snapshot_from_db(cfg)
    if not _marker_path(cfg).exists():
        touch_runtime_access_marker(cfg)
    _store_snapshot(snapshot, _read_marker_token(cfg))


def refresh_runtime_access_cache(cfg: AppConfig | None = None) -> None:
    cfg = cfg or get_config()
    snapshot = _load_snapshot_from_db(cfg)
    _store_snapshot(snapshot, _read_marker_token(cfg))


def _watcher_loop(stop_event: threading.Event, cfg: AppConfig) -> None:
    while not stop_event.wait(_WATCH_INTERVAL_SECONDS):
        try:
            marker_token = _read_marker_token(cfg)
            with _cache_lock:
                last_marker_token = _last_marker_token
            if marker_token == last_marker_token:
                continue
            refresh_runtime_access_cache(cfg)
        except Exception:
            _logger.exception("Runtime access cache refresh failed")


def start_runtime_access_cache_watcher(cfg: AppConfig | None = None) -> None:
    global _watcher_thread
    cfg = cfg or get_config()
    initialize_runtime_access_cache(cfg)
    _watcher_stop_event.clear()
    if _watcher_thread is not None and _watcher_thread.is_alive():
        return
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(_watcher_stop_event, cfg),
        name="arqs-runtime-access-cache",
        daemon=True,
    )
    _watcher_thread.start()


def stop_runtime_access_cache_watcher() -> None:
    global _watcher_thread
    _watcher_stop_event.set()
    if _watcher_thread is not None and _watcher_thread.is_alive():
        _watcher_thread.join(timeout=2.0)
    _watcher_thread = None


def get_runtime_settings_cached(cfg: AppConfig | None = None) -> dict[str, Any]:
    cfg = cfg or get_config()
    with _cache_lock:
        snapshot = _snapshot
    if snapshot is None:
        return _runtime_settings_defaults(cfg)
    return dict(snapshot.runtime_settings)


def get_inbox_limits_cached(cfg: AppConfig | None = None) -> tuple[int, int]:
    settings = get_runtime_settings_cached(cfg)
    return int(settings["long_poll_max_seconds"]), int(settings["max_inbox_batch"])


def is_ip_allowed_cached(ip: str, cfg: AppConfig | None = None) -> bool:
    cfg = cfg or get_config()
    with _cache_lock:
        snapshot = _snapshot

    if snapshot is None:
        mode = str(cfg.network.ip_access_mode).strip().lower()
        normalized_ip = _normalize_ip(ip)
        if mode == "off":
            return True
        if mode == "config":
            return not _static_config_ip_denied(cfg, normalized_ip)
        return not _static_config_ip_denied(cfg, normalized_ip)

    normalized_ip = _normalize_ip(ip)
    if snapshot.ip_access_mode == "off":
        return True
    if snapshot.ip_access_mode == "config":
        return not _static_config_ip_denied(cfg, normalized_ip)
    if normalized_ip in snapshot.allowed_ips:
        return True
    if normalized_ip in snapshot.denied_ips:
        return False
    if _static_config_ip_denied(cfg, normalized_ip):
        return False
    return snapshot.default_ip_policy == "allow"
