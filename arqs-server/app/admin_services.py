from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from .config import AppConfig
from .db import get_config
from .models import Delivery, DirectedRoute, Endpoint, Link, LinkCode, Node, Packet
from .runtime_access_cache import mark_runtime_access_dirty


RUNTIME_SETTINGS_ID = 1
_RUNTIME_SETTING_FIELDS = {
    "max_storage_bytes",
    "max_packet_bytes",
    "max_queued_packets_per_endpoint",
    "max_queued_bytes_per_endpoint",
    "max_queued_bytes_per_node",
    "max_total_queued_packets",
    "max_total_queued_bytes",
    "max_inbox_batch",
    "long_poll_max_seconds",
    "send_window_seconds",
    "max_sends_per_window",
    "default_ip_policy",
}
_RUNTIME_SETTING_INT_FIELDS = {
    "max_storage_bytes",
    "max_packet_bytes",
    "max_queued_packets_per_endpoint",
    "max_queued_bytes_per_endpoint",
    "max_queued_bytes_per_node",
    "max_total_queued_packets",
    "max_total_queued_bytes",
    "max_inbox_batch",
    "long_poll_max_seconds",
    "send_window_seconds",
    "max_sends_per_window",
}
_DEFAULT_IP_POLICY = "allow"
_IP_POLICY_VALUES = {"allow", "deny"}
_IP_ACCESS_MODE_VALUES = {"off", "config", "dynamic"}
_IP_RULE_ACTION_VALUES = {"allow", "deny"}
_NODE_STATUS_VALUES = {"active", "disabled", "revoked"}
_LINK_STATUS_VALUES = {"active", "revoked"}
_LINK_CODE_STATUS_VALUES = {"active", "used", "expired", "revoked"}
_ADMIN_TABLES_READY = False


class AdminError(Exception):
    """Base admin-service error."""


class AdminValidationError(AdminError):
    """Raised when an admin request is invalid."""


class AdminNotFoundError(AdminError):
    """Raised when a requested object does not exist."""


class AdminConflictError(AdminError):
    """Raised when the requested state transition is invalid."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _cleanup_expired(db: Session, cfg: AppConfig) -> dict[str, int]:
    from .services import cleanup_expired

    return cleanup_expired(db, cfg)


def _table_exists(db: Session, table_name: str) -> bool:
    row = db.execute(
        text(
            """
            SELECT 1
            FROM sqlite_master
            WHERE type = 'table' AND name = :table_name
            """
        ),
        {"table_name": table_name},
    ).first()
    return row is not None


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
        "updated_at": utcnow(),
    }


def _runtime_settings_column_defaults(cfg: AppConfig) -> dict[str, Any]:
    defaults = _runtime_settings_defaults(cfg)
    defaults.pop("settings_id", None)
    return defaults


def _runtime_settings_columns(db: Session) -> set[str]:
    rows = db.execute(text("PRAGMA table_info(runtime_settings)")).mappings().all()
    return {str(row["name"]) for row in rows}


def _try_normalize_ip(ip: str) -> str | None:
    raw = str(ip).strip()
    if not raw:
        return None
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError:
        return None


def _validate_ip_policy(value: Any, *, field_name: str = "default_ip_policy") -> str:
    normalized = str(value).strip().lower()
    if normalized not in _IP_POLICY_VALUES:
        allowed_display = ", ".join(sorted(_IP_POLICY_VALUES))
        raise AdminValidationError(f"{field_name} must be one of: {allowed_display}")
    return normalized


def _ensure_admin_tables(db: Session, cfg: AppConfig | None = None) -> None:
    global _ADMIN_TABLES_READY
    if _ADMIN_TABLES_READY:
        return
    ensure_admin_tables(db, cfg=cfg)


def _count_ip_rules(db: Session, action: str) -> int:
    return int(
        db.execute(
            text("SELECT COUNT(*) FROM ip_access_rules WHERE action = :action"),
            {"action": action},
        ).scalar_one()
    )


def _static_config_ip_denied(cfg: AppConfig, normalized_ip: str | None) -> bool:
    if normalized_ip is None:
        return False
    for candidate in cfg.blacklist.client_ips:
        if _try_normalize_ip(candidate) == normalized_ip:
            return True
    return False


def _active_packet_clause(*, now: datetime | None = None):
    current = now or utcnow()
    return or_(Packet.expires_at.is_(None), Packet.expires_at > current)


def _active_link_code_clause(*, now: datetime | None = None):
    current = now or utcnow()
    return and_(LinkCode.status == "active", LinkCode.expires_at > current)


def _effective_link_code_status(row: LinkCode, *, now: datetime | None = None) -> str:
    current = now or utcnow()
    if row.status == "active" and row.expires_at <= current:
        return "expired"
    return row.status


def _validate_ip_access_mode(value: Any) -> str:
    normalized = str(value).strip().lower()
    if normalized not in _IP_ACCESS_MODE_VALUES:
        allowed_display = ", ".join(sorted(_IP_ACCESS_MODE_VALUES))
        raise AdminValidationError(f"ip_access_mode must be one of: {allowed_display}")
    return normalized


def _ip_access_mode(cfg: AppConfig) -> str:
    return _validate_ip_access_mode(cfg.network.ip_access_mode)


def _get_runtime_settings_row(db: Session) -> dict[str, Any]:
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
    if row is None:
        raise AdminConflictError("runtime_settings row is missing")
    return dict(row)


def _get_runtime_settings_row_safe(db: Session, cfg: AppConfig | None = None) -> dict[str, Any]:
    _ensure_admin_tables(db, cfg)
    try:
        return _get_runtime_settings_row(db)
    except (AdminConflictError, OperationalError):
        ensure_admin_tables(db, cfg=cfg, force=True)
        return _get_runtime_settings_row(db)


def _db_file_size(cfg: AppConfig) -> int | None:
    db_path = Path(cfg.storage.db_path)
    candidates = [
        db_path,
        db_path.with_name(f"{db_path.name}-wal"),
        db_path.with_name(f"{db_path.name}-shm"),
        db_path.with_name(f"{db_path.name}-journal"),
    ]
    total = 0
    found_any = False
    for candidate in candidates:
        try:
            total += candidate.stat().st_size
            found_any = True
        except OSError:
            continue
    return total if found_any else None



def _validate_positive_int(name: str, value: Any) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AdminValidationError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise AdminValidationError(f"{name} must be > 0")
    return parsed



def normalize_ip(ip: str) -> str:
    raw = str(ip).strip()
    if not raw:
        raise AdminValidationError("ip is required")
    try:
        return str(ipaddress.ip_address(raw))
    except ValueError as exc:
        raise AdminValidationError(f"invalid IP address: {raw}") from exc



def _coerce_status_filter(value: str | None, *, allowed: set[str], field_name: str) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized not in allowed:
        allowed_display = ", ".join(sorted(allowed))
        raise AdminValidationError(f"{field_name} must be one of: {allowed_display}")
    return normalized



def ensure_admin_tables(db: Session, cfg: AppConfig | None = None, *, force: bool = False) -> None:
    global _ADMIN_TABLES_READY
    if _ADMIN_TABLES_READY and not force:
        return
    cfg = cfg or get_config()
    defaults = _runtime_settings_defaults(cfg)

    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS runtime_settings (
                settings_id INTEGER PRIMARY KEY,
                max_storage_bytes INTEGER NOT NULL,
                max_packet_bytes INTEGER NOT NULL,
                max_queued_packets_per_endpoint INTEGER NOT NULL,
                max_queued_bytes_per_endpoint INTEGER NOT NULL,
                max_queued_bytes_per_node INTEGER NOT NULL,
                max_total_queued_packets INTEGER NOT NULL,
                max_total_queued_bytes INTEGER NOT NULL,
                max_inbox_batch INTEGER NOT NULL,
                long_poll_max_seconds INTEGER NOT NULL,
                send_window_seconds INTEGER NOT NULL,
                max_sends_per_window INTEGER NOT NULL,
                default_ip_policy TEXT NOT NULL,
                updated_at DATETIME NOT NULL
            )
            """
        )
    )

    existing_columns = _runtime_settings_columns(db)
    column_defaults = _runtime_settings_column_defaults(cfg)
    missing_columns = {
        key: value
        for key, value in column_defaults.items()
        if key not in existing_columns
    }
    for column_name, default_value in missing_columns.items():
        if isinstance(default_value, str):
            literal = f"'{default_value}'"
        else:
            literal = str(default_value)
        db.execute(
            text(
                f"ALTER TABLE runtime_settings ADD COLUMN {column_name} "
                f"{'TEXT' if isinstance(default_value, str) else 'INTEGER'} NOT NULL DEFAULT {literal}"
            )
        )

    db.execute(
        text(
            """
            INSERT OR IGNORE INTO runtime_settings (
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
            ) VALUES (
                :settings_id,
                :max_storage_bytes,
                :max_packet_bytes,
                :max_queued_packets_per_endpoint,
                :max_queued_bytes_per_endpoint,
                :max_queued_bytes_per_node,
                :max_total_queued_packets,
                :max_total_queued_bytes,
                :max_inbox_batch,
                :long_poll_max_seconds,
                :send_window_seconds,
                :max_sends_per_window,
                :default_ip_policy,
                :updated_at
            )
            """
        ),
        defaults,
    )

    update_params = {"settings_id": RUNTIME_SETTINGS_ID}
    assignments: list[str] = []
    for column_name, default_value in column_defaults.items():
        assignments.append(f"{column_name} = COALESCE({column_name}, :{column_name})")
        update_params[column_name] = default_value
    db.execute(
        text(
            "UPDATE runtime_settings SET "
            + ", ".join(assignments)
            + " WHERE settings_id = :settings_id"
        ),
        update_params,
    )

    db.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS ip_access_rules (
                ip TEXT PRIMARY KEY,
                action TEXT NOT NULL,
                reason TEXT NULL,
                created_at DATETIME NOT NULL
            )
            """
        )
    )
    db.execute(text("CREATE INDEX IF NOT EXISTS ix_ip_access_rules_action ON ip_access_rules (action)"))
    db.execute(text("DROP INDEX IF EXISTS ix_ip_access_rules_expires_at"))

    if _table_exists(db, "blocked_ips"):
        legacy_rows = db.execute(
            text(
                """
                SELECT ip, reason, created_at
                FROM blocked_ips
                ORDER BY created_at ASC, ip ASC
                """
            )
        ).mappings().all()
        for row in legacy_rows:
            normalized_ip = _try_normalize_ip(str(row["ip"]))
            if normalized_ip is None:
                continue
            exists = db.execute(
                text("SELECT 1 FROM ip_access_rules WHERE ip = :ip"),
                {"ip": normalized_ip},
            ).first()
            if exists is not None:
                continue
            db.execute(
                text(
                    """
                    INSERT INTO ip_access_rules (ip, action, reason, created_at)
                    VALUES (:ip, 'deny', :reason, :created_at)
                    """
                ),
                {
                    "ip": normalized_ip,
                    "reason": row["reason"],
                    "created_at": row["created_at"],
                },
            )

    _ADMIN_TABLES_READY = True


# ---------------------------------------------------------------------------
# Public service functions
# ---------------------------------------------------------------------------


def get_runtime_settings(db: Session) -> dict[str, Any]:
    return _get_runtime_settings_row_safe(db)



def update_runtime_settings(db: Session, **updates: Any) -> dict[str, Any]:
    _ensure_admin_tables(db)
    filtered: dict[str, Any] = {}
    for key, value in updates.items():
        if value is None:
            continue
        if key not in _RUNTIME_SETTING_FIELDS:
            raise AdminValidationError(f"unsupported runtime setting: {key}")
        if key in _RUNTIME_SETTING_INT_FIELDS:
            filtered[key] = _validate_positive_int(key, value)
        elif key == "default_ip_policy":
            filtered[key] = _validate_ip_policy(value)
        else:
            raise AdminValidationError(f"unsupported runtime setting: {key}")

    if not filtered:
        raise AdminValidationError("no runtime setting fields were provided")

    if filtered.get("default_ip_policy") == "deny" and _count_ip_rules(db, "allow") == 0:
        raise AdminConflictError("cannot set default_ip_policy=deny without at least one explicit allow rule")

    filtered["updated_at"] = utcnow()
    assignments = ", ".join(f"{field} = :{field}" for field in filtered.keys())
    filtered["settings_id"] = RUNTIME_SETTINGS_ID

    db.execute(
        text(f"UPDATE runtime_settings SET {assignments} WHERE settings_id = :settings_id"),
        filtered,
    )
    mark_runtime_access_dirty(db)
    return _get_runtime_settings_row_safe(db)



def get_ip_policy(db: Session) -> dict[str, Any]:
    settings = get_runtime_settings(db)
    return {
        "default_ip_policy": settings["default_ip_policy"],
        "updated_at": settings["updated_at"],
    }


def set_ip_policy(db: Session, policy: str) -> dict[str, Any]:
    settings = update_runtime_settings(db, default_ip_policy=policy)
    return {
        "default_ip_policy": settings["default_ip_policy"],
        "updated_at": settings["updated_at"],
    }


def list_ip_rules(db: Session, *, action: str | None = None) -> list[dict[str, Any]]:
    _ensure_admin_tables(db)
    params: dict[str, Any] = {}
    where = ""
    if action is not None:
        normalized_action = _validate_ip_policy(action, field_name="action")
        where = "WHERE action = :action"
        params["action"] = normalized_action
    rows = db.execute(
        text(
            """
            SELECT ip, action, reason, created_at
            FROM ip_access_rules
            """
            + where
            + """
            ORDER BY created_at DESC, ip ASC
            """
        ),
        params,
    ).mappings().all()
    return [dict(row) for row in rows]



def set_ip_rule(
    db: Session,
    ip: str,
    *,
    action: str,
    reason: str | None = None,
) -> dict[str, Any]:
    _ensure_admin_tables(db)
    normalized_ip = normalize_ip(ip)
    normalized_action = _validate_ip_policy(action, field_name="action")
    now = utcnow()
    db.execute(
        text(
            """
            INSERT INTO ip_access_rules (ip, action, reason, created_at)
            VALUES (:ip, :action, :reason, :created_at)
            ON CONFLICT(ip) DO UPDATE SET
                action = excluded.action,
                reason = excluded.reason,
                created_at = excluded.created_at
            """
        ),
        {
            "ip": normalized_ip,
            "action": normalized_action,
            "reason": (reason.strip() if isinstance(reason, str) and reason.strip() else None),
            "created_at": now,
        },
    )
    mark_runtime_access_dirty(db)
    return {
        "ip": normalized_ip,
        "action": normalized_action,
        "reason": (reason.strip() if isinstance(reason, str) and reason.strip() else None),
        "created_at": now,
    }



def allow_ip(
    db: Session,
    ip: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    result = set_ip_rule(db, ip, action="allow", reason=reason)
    return {"allowed": True, **result}


def deny_ip(
    db: Session,
    ip: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    result = set_ip_rule(db, ip, action="deny", reason=reason)
    return {"denied": True, **result}


def remove_ip_rule(db: Session, ip: str) -> dict[str, Any]:
    _ensure_admin_tables(db)
    normalized_ip = normalize_ip(ip)
    result = db.execute(text("DELETE FROM ip_access_rules WHERE ip = :ip"), {"ip": normalized_ip})
    if int(result.rowcount or 0) == 0:
        raise AdminNotFoundError(f"IP rule not found: {normalized_ip}")
    mark_runtime_access_dirty(db)
    return {"removed": True, "ip": normalized_ip}



def block_ip(
    db: Session,
    ip: str,
    *,
    reason: str | None = None,
) -> dict[str, Any]:
    result = set_ip_rule(db, ip, action="deny", reason=reason)
    return {"blocked": True, **result}


def pardon_ip(db: Session, ip: str) -> dict[str, Any]:
    result = remove_ip_rule(db, ip)
    return {"pardoned": True, "ip": result["ip"]}


def list_blocked_ips(db: Session) -> list[dict[str, Any]]:
    return list_ip_rules(db, action="deny")


def is_ip_allowed(db: Session, ip: str, cfg: AppConfig | None = None) -> bool:
    cfg = cfg or get_config()
    mode = _ip_access_mode(cfg)
    if mode == "off":
        return True
    normalized_ip = _try_normalize_ip(ip)
    if mode == "config":
        return not _static_config_ip_denied(cfg, normalized_ip)

    settings = get_runtime_settings(db)
    if normalized_ip is not None:
        row = db.execute(
            text("SELECT action FROM ip_access_rules WHERE ip = :ip"),
            {"ip": normalized_ip},
        ).mappings().first()
        if row is not None:
            return str(row["action"]) == "allow"
        if _static_config_ip_denied(cfg, normalized_ip):
            return False
    return settings["default_ip_policy"] == "allow"


def is_ip_blocked(db: Session, ip: str, cfg: AppConfig | None = None) -> bool:
    cfg = cfg or get_config()
    mode = _ip_access_mode(cfg)
    if mode == "off":
        return False
    normalized_ip = _try_normalize_ip(ip)
    if mode == "config" or normalized_ip is None:
        return _static_config_ip_denied(cfg, normalized_ip)
    _ensure_admin_tables(db, cfg)
    if normalized_ip is None:
        return False
    row = db.execute(
        text("SELECT action FROM ip_access_rules WHERE ip = :ip"),
        {"ip": normalized_ip},
    ).mappings().first()
    if row is not None:
        return str(row["action"]) == "deny"
    return _static_config_ip_denied(cfg, normalized_ip)



def health_check(db: Session) -> dict[str, Any]:
    cfg = get_config()
    _ensure_admin_tables(db, cfg)
    db.execute(select(1)).scalar_one()
    return {
        "status": "ok",
        "app": cfg.server.app_name,
        "db_path": cfg.storage.db_path,
        "db_file_size_bytes": _db_file_size(cfg),
        "time": utcnow(),
        "maintenance": {
            "cleanup_interval_seconds": int(cfg.maintenance.cleanup_interval_seconds),
            "enabled": int(cfg.maintenance.cleanup_interval_seconds) > 0,
        },
        "runtime_settings": _get_runtime_settings_row_safe(db, cfg),
        "ip_access": {
            "mode": _ip_access_mode(cfg),
            "default_ip_policy": get_runtime_settings(db)["default_ip_policy"],
            "allow_rules_total": _count_ip_rules(db, "allow"),
            "deny_rules_total": _count_ip_rules(db, "deny"),
        },
    }



def get_summary_stats(db: Session) -> dict[str, Any]:
    cfg = get_config()
    _ensure_admin_tables(db, cfg)
    now = utcnow()
    active_packets = _active_packet_clause(now=now)

    queued_bytes_total = int(
        db.scalar(
            select(func.coalesce(func.sum(Packet.payload_bytes), 0))
            .select_from(Delivery)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(active_packets)
        )
        or 0
    )

    return {
        "nodes_total": int(db.scalar(select(func.count()).select_from(Node)) or 0),
        "endpoints_total": int(db.scalar(select(func.count()).select_from(Endpoint)) or 0),
        "active_links_total": int(db.scalar(select(func.count()).select_from(Link).where(Link.status == "active")) or 0),
        "queued_packets_total": int(
            db.scalar(
                select(func.count(Delivery.delivery_id))
                .select_from(Delivery)
                .join(Packet, Packet.packet_id == Delivery.packet_id)
                .where(active_packets)
            )
            or 0
        ),
        "queued_bytes_total": queued_bytes_total,
        "active_link_codes_total": int(
            db.scalar(select(func.count()).select_from(LinkCode).where(_active_link_code_clause(now=now))) or 0
        ),
        "default_ip_policy": get_runtime_settings(db)["default_ip_policy"],
        "allowed_ips_total": _count_ip_rules(db, "allow"),
        "denied_ips_total": _count_ip_rules(db, "deny"),
    }



def get_queue_stats_by_node(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    now = utcnow()
    active_packets = _active_packet_clause(now=now)
    limit = _validate_positive_int("limit", limit)

    rows = db.execute(
        select(
            Delivery.destination_node_id.label("node_id"),
            func.count(Delivery.delivery_id).label("queued_packets"),
            func.coalesce(func.sum(Packet.payload_bytes), 0).label("queued_bytes"),
            func.min(Delivery.queued_at).label("oldest_queued_at"),
        )
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(active_packets)
        .group_by(Delivery.destination_node_id)
        .order_by(func.count(Delivery.delivery_id).desc(), func.coalesce(func.sum(Packet.payload_bytes), 0).desc())
        .limit(limit)
    ).all()

    return [
        {
            "node_id": row.node_id,
            "queued_packets": int(row.queued_packets),
            "queued_bytes": int(row.queued_bytes),
            "oldest_queued_at": row.oldest_queued_at,
        }
        for row in rows
    ]



def get_queue_stats_by_endpoint(db: Session, *, limit: int = 50) -> list[dict[str, Any]]:
    now = utcnow()
    active_packets = _active_packet_clause(now=now)
    limit = _validate_positive_int("limit", limit)

    rows = db.execute(
        select(
            Delivery.destination_endpoint_id.label("endpoint_id"),
            func.count(Delivery.delivery_id).label("queued_packets"),
            func.coalesce(func.sum(Packet.payload_bytes), 0).label("queued_bytes"),
            func.min(Delivery.queued_at).label("oldest_queued_at"),
        )
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(active_packets)
        .group_by(Delivery.destination_endpoint_id)
        .order_by(func.count(Delivery.delivery_id).desc(), func.coalesce(func.sum(Packet.payload_bytes), 0).desc())
        .limit(limit)
    ).all()

    return [
        {
            "endpoint_id": row.endpoint_id,
            "queued_packets": int(row.queued_packets),
            "queued_bytes": int(row.queued_bytes),
            "oldest_queued_at": row.oldest_queued_at,
        }
        for row in rows
    ]



def get_oldest_queued_delivery_info(db: Session) -> dict[str, Any]:
    now = utcnow()
    row = db.execute(
        select(Delivery, Packet)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(_active_packet_clause(now=now))
        .order_by(Delivery.queued_at.asc())
        .limit(1)
    ).first()
    if row is None:
        return {"queued": False}
    delivery, packet = row
    return {
        "queued": True,
        "delivery_id": delivery.delivery_id,
        "packet_id": delivery.packet_id,
        "destination_node_id": delivery.destination_node_id,
        "destination_endpoint_id": delivery.destination_endpoint_id,
        "queued_at": delivery.queued_at,
        "state": delivery.state,
        "payload_bytes": packet.payload_bytes if packet is not None else None,
    }



def list_nodes(db: Session, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit = _validate_positive_int("limit", limit)
    status = _coerce_status_filter(status, allowed=_NODE_STATUS_VALUES, field_name="status")

    stmt = select(Node).order_by(Node.created_at.desc()).limit(limit)
    if status is not None:
        stmt = select(Node).where(Node.status == status).order_by(Node.created_at.desc()).limit(limit)

    rows = db.execute(stmt).scalars().all()
    return [
        {
            "node_id": row.node_id,
            "node_name": row.node_name,
            "created_at": row.created_at,
            "status": row.status,
        }
        for row in rows
    ]



def get_node_detail(db: Session, node_id: str) -> dict[str, Any]:
    node = db.get(Node, node_id)
    if node is None:
        raise AdminNotFoundError(f"node not found: {node_id}")
    now = utcnow()

    endpoint_ids = [
        row[0]
        for row in db.execute(select(Endpoint.endpoint_id).where(Endpoint.node_id == node.node_id)).all()
    ]

    inbound_queue_stats = db.execute(
        select(
            func.count(Delivery.delivery_id),
            func.coalesce(func.sum(Packet.payload_bytes), 0),
        )
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(Delivery.destination_node_id == node.node_id, _active_packet_clause(now=now))
    ).one()

    link_count = 0
    link_code_count = 0
    if endpoint_ids:
        link_count = int(
            db.scalar(
                select(func.count()).select_from(Link).where(
                    Link.status == "active",
                    (Link.endpoint_a_id.in_(endpoint_ids)) | (Link.endpoint_b_id.in_(endpoint_ids)),
                )
            )
            or 0
        )
        link_code_count = int(
            db.scalar(
                select(func.count()).select_from(LinkCode).where(
                    _active_link_code_clause(now=now),
                    LinkCode.source_endpoint_id.in_(endpoint_ids),
                )
            )
            or 0
        )

    return {
        "node_id": node.node_id,
        "node_name": node.node_name,
        "created_at": node.created_at,
        "status": node.status,
        "endpoint_count": len(endpoint_ids),
        "active_link_count": link_count,
        "active_link_code_count": link_code_count,
        "queued_inbound_packets": int(inbound_queue_stats[0] or 0),
        "queued_inbound_bytes": int(inbound_queue_stats[1] or 0),
    }



def set_node_status(db: Session, node_id: str, *, status: str) -> dict[str, Any]:
    normalized = _coerce_status_filter(status, allowed=_NODE_STATUS_VALUES, field_name="status")
    assert normalized is not None
    node = db.get(Node, node_id)
    if node is None:
        raise AdminNotFoundError(f"node not found: {node_id}")
    node.status = normalized
    db.add(node)
    return {"node_id": node_id, "status": normalized}



def disable_node(db: Session, node_id: str) -> dict[str, Any]:
    result = set_node_status(db, node_id, status="disabled")
    return {"disabled": True, **result}



def enable_node(db: Session, node_id: str) -> dict[str, Any]:
    result = set_node_status(db, node_id, status="active")
    return {"enabled": True, **result}



def revoke_node(db: Session, node_id: str) -> dict[str, Any]:
    result = set_node_status(db, node_id, status="revoked")
    return {"revoked": True, **result}



def list_endpoints(
    db: Session,
    *,
    node_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    limit = _validate_positive_int("limit", limit)
    status = _coerce_status_filter(status, allowed=_NODE_STATUS_VALUES, field_name="status")

    stmt = select(Endpoint)
    if node_id is not None:
        stmt = stmt.where(Endpoint.node_id == node_id)
    if status is not None:
        stmt = stmt.where(Endpoint.status == status)
    stmt = stmt.order_by(Endpoint.created_at.desc()).limit(limit)

    rows = db.execute(stmt).scalars().all()
    return [
        {
            "endpoint_id": row.endpoint_id,
            "node_id": row.node_id,
            "endpoint_name": row.endpoint_name,
            "kind": row.kind,
            "meta": row.meta,
            "created_at": row.created_at,
            "status": row.status,
        }
        for row in rows
    ]



def get_endpoint_detail(db: Session, endpoint_id: str) -> dict[str, Any]:
    endpoint = db.get(Endpoint, endpoint_id)
    if endpoint is None:
        raise AdminNotFoundError(f"endpoint not found: {endpoint_id}")
    now = utcnow()

    queue_stats = db.execute(
        select(
            func.count(Delivery.delivery_id),
            func.coalesce(func.sum(Packet.payload_bytes), 0),
        )
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(Delivery.destination_endpoint_id == endpoint.endpoint_id, _active_packet_clause(now=now))
    ).one()

    active_link_count = int(
        db.scalar(
            select(func.count()).select_from(Link).where(
                Link.status == "active",
                (Link.endpoint_a_id == endpoint.endpoint_id) | (Link.endpoint_b_id == endpoint.endpoint_id),
            )
        )
        or 0
    )

    return {
        "endpoint_id": endpoint.endpoint_id,
        "node_id": endpoint.node_id,
        "endpoint_name": endpoint.endpoint_name,
        "kind": endpoint.kind,
        "meta": endpoint.meta,
        "created_at": endpoint.created_at,
        "status": endpoint.status,
        "queued_inbound_packets": int(queue_stats[0] or 0),
        "queued_inbound_bytes": int(queue_stats[1] or 0),
        "active_link_count": active_link_count,
    }



def list_links_admin(db: Session, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    limit = _validate_positive_int("limit", limit)
    status = _coerce_status_filter(status, allowed=_LINK_STATUS_VALUES, field_name="status")

    stmt = select(Link)
    if status is not None:
        stmt = stmt.where(Link.status == status)
    stmt = stmt.order_by(Link.created_at.desc()).limit(limit)

    rows = db.execute(stmt).scalars().all()
    return [
        {
            "link_id": row.link_id,
            "endpoint_a_id": row.endpoint_a_id,
            "endpoint_b_id": row.endpoint_b_id,
            "mode": row.mode,
            "created_at": row.created_at,
            "status": row.status,
        }
        for row in rows
    ]



def revoke_link_admin(db: Session, link_id: str) -> dict[str, Any]:
    row = db.get(Link, link_id)
    if row is None:
        raise AdminNotFoundError(f"link not found: {link_id}")

    routes = db.execute(select(DirectedRoute).where(DirectedRoute.created_by_link_id == link_id)).scalars().all()
    row.status = "revoked"
    for route in routes:
        route.status = "revoked"
        db.add(route)
    db.add(row)
    return {
        "revoked": True,
        "link_id": link_id,
        "routes_revoked": len(routes),
    }



def list_link_codes_admin(db: Session, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    now = utcnow()
    limit = _validate_positive_int("limit", limit)
    status = _coerce_status_filter(status, allowed=_LINK_CODE_STATUS_VALUES, field_name="status")

    stmt = select(LinkCode)
    if status is not None:
        if status == "active":
            stmt = stmt.where(_active_link_code_clause(now=now))
        elif status == "expired":
            stmt = stmt.where(
                or_(
                    LinkCode.status == "expired",
                    and_(LinkCode.status == "active", LinkCode.expires_at <= now),
                )
            )
        else:
            stmt = stmt.where(LinkCode.status == status)
    stmt = stmt.order_by(LinkCode.created_at.desc()).limit(limit)

    rows = db.execute(stmt).scalars().all()
    return [
        {
            "link_code_id": row.link_code_id,
            "code": row.code,
            "source_endpoint_id": row.source_endpoint_id,
            "requested_mode": row.requested_mode,
            "created_at": row.created_at,
            "expires_at": row.expires_at,
            "status": _effective_link_code_status(row, now=now),
        }
        for row in rows
    ]



def run_cleanup_now(db: Session) -> dict[str, Any]:
    cfg = get_config()
    _ensure_admin_tables(db, cfg)
    return _cleanup_expired(db, cfg)


__all__ = [
    "AdminError",
    "AdminValidationError",
    "AdminNotFoundError",
    "AdminConflictError",
    "normalize_ip",
    "ensure_admin_tables",
    "get_runtime_settings",
    "update_runtime_settings",
    "get_ip_policy",
    "set_ip_policy",
    "list_ip_rules",
    "set_ip_rule",
    "allow_ip",
    "deny_ip",
    "remove_ip_rule",
    "list_blocked_ips",
    "block_ip",
    "pardon_ip",
    "is_ip_allowed",
    "is_ip_blocked",
    "health_check",
    "get_summary_stats",
    "get_queue_stats_by_node",
    "get_queue_stats_by_endpoint",
    "get_oldest_queued_delivery_info",
    "list_nodes",
    "get_node_detail",
    "set_node_status",
    "disable_node",
    "enable_node",
    "revoke_node",
    "list_endpoints",
    "get_endpoint_detail",
    "list_links_admin",
    "revoke_link_admin",
    "list_link_codes_admin",
    "run_cleanup_now",
]
