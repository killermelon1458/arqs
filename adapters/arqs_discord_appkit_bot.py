from __future__ import annotations

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional at import time
    def load_dotenv(*args: object, **kwargs: object) -> bool:
        return False

load_dotenv()

import argparse
import asyncio
import contextlib
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal


def _bootstrap_local_imports() -> None:
    script_dir = Path(__file__).resolve().parent
    cwd = Path.cwd().resolve()
    candidates = [
        script_dir.parent / "apis",
        cwd,
        cwd / "apis",
        script_dir,
    ]
    valid_candidates: list[Path] = []
    for candidate in candidates:
        if not candidate.exists():
            continue
        has_appkit = (candidate / "appkit").is_dir()
        has_arqs_modules = (candidate / "arqs_api.py").is_file() and (candidate / "arqs_conventions.py").is_file()
        if not (has_appkit or has_arqs_modules):
            continue
        valid_candidates.append(candidate)
    for candidate in reversed(valid_candidates):
        candidate_str = str(candidate)
        if candidate_str not in sys.path:
            sys.path.insert(0, candidate_str)


_bootstrap_local_imports()

try:  # pragma: no cover - runtime dependency
    import discord
    from discord import app_commands
    from discord.ext import commands
except ImportError as exc:  # pragma: no cover - optional at import time
    discord = None
    app_commands = None
    commands = None
    _DISCORD_IMPORT_ERROR: Exception | None = exc
else:  # pragma: no cover - trivial
    _DISCORD_IMPORT_ERROR = None

from appkit import ARQSApp
from arqs_api import ARQSError, ARQSHTTPError, Delivery, Endpoint, Link, LinkCode
from arqs_conventions import (
    TYPE_COMMAND_RESPONSE_V1,
    TYPE_COMMAND_V1,
    TYPE_MESSAGE_V1,
    TYPE_NOTIFICATION_V1,
    TYPE_REACTION_V1,
    TYPE_RECEIPT_READ_V1,
    TYPE_RECEIPT_RECEIVED_V1,
    get_causation_id,
    get_correlation_id,
    get_reaction_key,
    is_receipt_type,
    render_packet_text,
)


APP_NAME = "discord-adapter"
READ_RECEIPT_EMOJI = "✅"
MAX_DISCORD_MESSAGE_LENGTH = 1900
RECONCILE_INTERVAL_SECONDS = 5
SEEN_DELIVERIES_LIMIT = 10000
RECEIPT_MODES = {
    "off",
    "discord_delivered",
    "reaction_read",
    "discord_delivered_and_reaction_read",
}
USER_LINK_MODE = Literal["bidirectional", "send_only", "receive_only"]
ARQS_LINK_MODE = Literal["bidirectional", "a_to_b", "b_to_a"]

DEFAULT_CONFIG_TEMPLATE = {
    "app_name": APP_NAME,
    "node_name": APP_NAME,
    "default_endpoint_name": "discord-control",
    "default_endpoint_kind": "discord_control",
    "transport_policy": "prefer_https",
    "delivery_mode": "queued",
    "retry_policy": "until_expired",
    "max_attempts": 20,
    "expires_after_seconds": 86400,
    "poll_wait_seconds": 20,
    "poll_limit": 100,
    "discord_sync_commands_on_start": False,
    "discord_log_level": "INFO",
    "receipt_default_mode": "off",
}

logger = logging.getLogger("arqs.discord")
state_logger = logging.getLogger("arqs.discord.state")
links_logger = logging.getLogger("arqs.discord.links")
receipts_logger = logging.getLogger("arqs.discord.receipts")
reactions_logger = logging.getLogger("arqs.discord.reactions")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_datetime(value: str | None) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def split_discord_message(content: str, *, limit: int = MAX_DISCORD_MESSAGE_LENGTH) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        window = remaining[:limit]
        split_at = window.rfind("\n")
        if split_at <= 0:
            split_at = window.rfind(" ")
        if split_at <= 0:
            split_at = limit
        chunk = remaining[:split_at].rstrip()
        if not chunk:
            chunk = remaining[:limit]
            split_at = limit
        chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def make_hidden_endpoint_name(discord_user_id: str) -> str:
    return f"discord:dm:{discord_user_id}:{uuid.uuid4().hex[:8]}"


def user_display_name(user: discord.abc.User) -> str:
    return str(user)


def normalize_mode(mode: str) -> str:
    value = str(mode or "").strip()
    if value not in RECEIPT_MODES:
        raise ValueError(f"unsupported receipt mode: {value}")
    return value


def user_mode_to_arqs_mode(mode: str) -> ARQS_LINK_MODE:
    mapping: dict[str, ARQS_LINK_MODE] = {
        "bidirectional": "bidirectional",
        "send_only": "a_to_b",
        "receive_only": "b_to_a",
    }
    return mapping[str(mode)]


def calculate_direction(link: Link, local_endpoint_id: str) -> tuple[bool, bool]:
    mode = str(link.mode)
    endpoint_a_id = str(link.endpoint_a_id)
    endpoint_b_id = str(link.endpoint_b_id)
    if mode == "bidirectional":
        return True, True
    if mode == "a_to_b":
        return local_endpoint_id == endpoint_a_id, local_endpoint_id == endpoint_b_id
    if mode == "b_to_a":
        return local_endpoint_id == endpoint_b_id, local_endpoint_id == endpoint_a_id
    return True, True


def describe_direction(can_send: bool, can_receive: bool) -> str:
    if can_send and can_receive:
        return "bidirectional"
    if can_send:
        return "send-only"
    if can_receive:
        return "receive-only"
    return "inactive"


def binding_direction(binding: "Binding") -> str:
    return describe_direction(binding.can_send, binding.can_receive)


def resolve_remote_endpoint(link: Link, local_endpoint_id: str) -> str:
    endpoint_a_id = str(link.endpoint_a_id)
    endpoint_b_id = str(link.endpoint_b_id)
    return endpoint_b_id if endpoint_a_id == local_endpoint_id else endpoint_a_id


def send_result_label(status: str) -> str:
    if status in {"accepted", "duplicate"}:
        return "sent"
    if status in {"queued", "background_queued"}:
        return "queued"
    if status == "missing":
        return "queued"
    return status


def serialize_discord_emoji(emoji: Any) -> dict[str, Any]:
    emoji_id = getattr(emoji, "id", None)
    emoji_name = str(getattr(emoji, "name", None) or str(emoji) or "").strip()
    payload: dict[str, Any] = {
        "emoji_name": emoji_name,
    }
    if emoji_id in (None, ""):
        payload["emoji"] = str(emoji)
    else:
        payload["emoji_id"] = str(emoji_id)
        payload["animated"] = bool(getattr(emoji, "animated", False))
    return payload


def reaction_display_key(reaction: dict[str, Any] | None) -> str:
    payload = dict(reaction or {})
    emoji_id = str(payload.get("emoji_id") or "").strip()
    if emoji_id:
        return f"id:{emoji_id}"
    emoji = str(payload.get("emoji") or "").strip()
    if emoji:
        return f"emoji:{emoji}"
    emoji_name = str(payload.get("emoji_name") or "").strip()
    if emoji_name:
        return f"name:{emoji_name}"
    return ""


def reaction_matches(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    left_key = reaction_display_key(left)
    right_key = reaction_display_key(right)
    return bool(left_key and right_key and left_key == right_key)


@dataclass
class Binding:
    binding_id: str
    discord_user_id: str
    local_endpoint_id: str
    remote_endpoint_id: str
    link_id: str
    label: str
    link_mode: str
    can_send: bool
    can_receive: bool
    status: str
    created_at: str
    updated_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Binding":
        return cls(
            binding_id=str(payload["binding_id"]),
            discord_user_id=str(payload["discord_user_id"]),
            local_endpoint_id=str(payload["local_endpoint_id"]),
            remote_endpoint_id=str(payload["remote_endpoint_id"]),
            link_id=str(payload["link_id"]),
            label=str(payload["label"]),
            link_mode=str(payload.get("link_mode") or "bidirectional"),
            can_send=bool(payload.get("can_send", True)),
            can_receive=bool(payload.get("can_receive", True)),
            status=str(payload.get("status") or "active"),
            created_at=str(payload["created_at"]),
            updated_at=str(payload["updated_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "discord_user_id": self.discord_user_id,
            "local_endpoint_id": self.local_endpoint_id,
            "remote_endpoint_id": self.remote_endpoint_id,
            "link_id": self.link_id,
            "label": self.label,
            "link_mode": self.link_mode,
            "can_send": self.can_send,
            "can_receive": self.can_receive,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class PendingLink:
    pending_id: str
    discord_user_id: str
    local_endpoint_id: str
    code: str
    requested_mode: str
    label: str
    created_at: str
    expires_at: str

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PendingLink":
        return cls(
            pending_id=str(payload["pending_id"]),
            discord_user_id=str(payload["discord_user_id"]),
            local_endpoint_id=str(payload["local_endpoint_id"]),
            code=str(payload["code"]),
            requested_mode=str(payload.get("requested_mode") or "bidirectional"),
            label=str(payload["label"]),
            created_at=str(payload["created_at"]),
            expires_at=str(payload["expires_at"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "pending_id": self.pending_id,
            "discord_user_id": self.discord_user_id,
            "local_endpoint_id": self.local_endpoint_id,
            "code": self.code,
            "requested_mode": self.requested_mode,
            "label": self.label,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
        }


class DiscordBridgeState:
    def __init__(self, path: Path) -> None:
        self.path = path
        raw = self._load_raw()
        self.bindings = [Binding.from_dict(item) for item in raw.get("bindings", []) if isinstance(item, dict)]
        self.pending_links = [PendingLink.from_dict(item) for item in raw.get("pending_links", []) if isinstance(item, dict)]
        self.active_contacts = {str(k): str(v) for k, v in dict(raw.get("active_contacts", {})).items()}
        self.seen_deliveries = [str(item) for item in list(raw.get("seen_deliveries", [])) if item]
        self.reply_index = {
            str(k): {str(inner_k): str(inner_v) for inner_k, inner_v in dict(v).items()}
            for k, v in dict(raw.get("reply_index", {})).items()
            if isinstance(v, dict)
        }
        self.receipt_settings = {
            str(k): {
                "default_mode": str(dict(v).get("default_mode") or "off"),
                "contacts": {str(inner_k): str(inner_v) for inner_k, inner_v in dict(dict(v).get("contacts", {})).items()},
            }
            for k, v in dict(raw.get("receipt_settings", {})).items()
            if isinstance(v, dict)
        }
        self.receipt_index = {
            str(k): dict(v)
            for k, v in dict(raw.get("receipt_index", {})).items()
            if isinstance(v, dict)
        }
        self.outbound_message_index = {
            str(k): dict(v)
            for k, v in dict(raw.get("outbound_message_index", {})).items()
            if isinstance(v, dict)
        }
        self._seen_delivery_ids = set(self.seen_deliveries)

    def _load_raw(self) -> dict[str, Any]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError:
            return {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "bindings": [binding.to_dict() for binding in self.bindings],
            "pending_links": [pending.to_dict() for pending in self.pending_links],
            "active_contacts": dict(sorted(self.active_contacts.items())),
            "seen_deliveries": self.seen_deliveries[-SEEN_DELIVERIES_LIMIT:],
            "reply_index": dict(sorted(self.reply_index.items())),
            "receipt_settings": dict(sorted(self.receipt_settings.items())),
            "receipt_index": dict(sorted(self.receipt_index.items())),
            "outbound_message_index": dict(sorted(self.outbound_message_index.items())),
        }
        self.path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def list_user_bindings(self, discord_user_id: str, *, include_inactive: bool = False) -> list[Binding]:
        items = [binding for binding in self.bindings if binding.discord_user_id == str(discord_user_id)]
        if not include_inactive:
            items = [binding for binding in items if binding.status == "active"]
        return sorted(items, key=lambda item: (parse_datetime(item.created_at) or utc_now(), item.label.lower()))

    def get_binding(self, binding_id: str) -> Binding | None:
        for binding in self.bindings:
            if binding.binding_id == str(binding_id):
                return binding
        return None

    def get_binding_by_link_id(self, link_id: str) -> Binding | None:
        for binding in self.bindings:
            if binding.link_id == str(link_id):
                return binding
        return None

    def get_binding_by_local_endpoint(self, endpoint_id: str) -> Binding | None:
        for binding in self.bindings:
            if binding.local_endpoint_id == str(endpoint_id):
                return binding
        return None

    def set_active_binding(self, discord_user_id: str, binding_id: str | None) -> None:
        key = str(discord_user_id)
        if binding_id in (None, ""):
            self.active_contacts.pop(key, None)
            return
        self.active_contacts[key] = str(binding_id)

    def get_active_binding(self, discord_user_id: str) -> Binding | None:
        active_id = self.active_contacts.get(str(discord_user_id))
        if not active_id:
            return None
        binding = self.get_binding(active_id)
        if binding is None or binding.status != "active":
            return None
        return binding

    def ensure_valid_active_binding(self, discord_user_id: str) -> Binding | None:
        binding = self.get_active_binding(discord_user_id)
        if binding is None:
            self.active_contacts.pop(str(discord_user_id), None)
        return binding

    def next_contact_label(self, discord_user_id: str) -> str:
        existing = {binding.label.casefold() for binding in self.list_user_bindings(discord_user_id, include_inactive=True)}
        index = 1
        while True:
            candidate = f"Contact {index}"
            if candidate.casefold() not in existing:
                return candidate
            index += 1

    def ensure_unique_label(
        self,
        discord_user_id: str,
        desired: str,
        *,
        exclude_binding_id: str | None = None,
        include_inactive: bool = True,
    ) -> str:
        candidate = str(desired or "").strip()
        if not candidate:
            return self.next_contact_label(discord_user_id)
        taken = {
            binding.label.casefold()
            for binding in self.list_user_bindings(discord_user_id, include_inactive=include_inactive)
            if binding.binding_id != exclude_binding_id
        }
        if candidate.casefold() not in taken:
            return candidate
        return self.next_contact_label(discord_user_id)

    def add_pending_link(self, pending: PendingLink) -> None:
        self.pending_links = [item for item in self.pending_links if item.pending_id != pending.pending_id]
        self.pending_links.append(pending)

    def remove_pending_link(self, pending_id: str) -> None:
        self.pending_links = [item for item in self.pending_links if item.pending_id != str(pending_id)]

    def prune_expired_pending_links(self) -> list[PendingLink]:
        now = utc_now()
        expired: list[PendingLink] = []
        remaining: list[PendingLink] = []
        for pending in self.pending_links:
            expires_at = parse_datetime(pending.expires_at)
            if expires_at is not None and expires_at <= now:
                expired.append(pending)
            else:
                remaining.append(pending)
        self.pending_links = remaining
        return expired

    def mark_seen_delivery(self, delivery_id: str) -> None:
        normalized = str(delivery_id)
        if normalized in self._seen_delivery_ids:
            return
        self._seen_delivery_ids.add(normalized)
        self.seen_deliveries.append(normalized)
        if len(self.seen_deliveries) > SEEN_DELIVERIES_LIMIT:
            trim = self.seen_deliveries[-SEEN_DELIVERIES_LIMIT:]
            self.seen_deliveries = trim
            self._seen_delivery_ids = set(trim)

    def has_seen_delivery(self, delivery_id: str) -> bool:
        return str(delivery_id) in self._seen_delivery_ids

    def remember_reply_messages(self, *, discord_user_id: str, binding_id: str, packet_id: str, message_ids: list[int]) -> None:
        for message_id in message_ids:
            self.reply_index[str(message_id)] = {
                "discord_user_id": str(discord_user_id),
                "binding_id": str(binding_id),
                "packet_id": str(packet_id),
            }

    def remember_receipt_messages(
        self,
        *,
        discord_user_id: str,
        binding_id: str,
        original_packet_id: str,
        original_from_endpoint_id: str,
        original_to_endpoint_id: str,
        original_correlation_id: str | None,
        message_ids: list[int],
    ) -> None:
        now_iso = to_iso(utc_now())
        for message_id in message_ids:
            self.receipt_index[str(message_id)] = {
                "discord_user_id": str(discord_user_id),
                "binding_id": str(binding_id),
                "original_packet_id": str(original_packet_id),
                "original_from_endpoint_id": str(original_from_endpoint_id),
                "original_to_endpoint_id": str(original_to_endpoint_id),
                "original_correlation_id": None if original_correlation_id in (None, "") else str(original_correlation_id),
                "read_receipt_sent": False,
                "delivered_receipt_sent": False,
                "created_at": now_iso,
            }

    def remember_outbound_message(
        self,
        *,
        packet_id: str,
        discord_user_id: str,
        discord_message_id: str,
        discord_channel_id: str,
        binding_id: str,
    ) -> None:
        self.outbound_message_index[str(packet_id)] = {
            "packet_id": str(packet_id),
            "discord_user_id": str(discord_user_id),
            "discord_message_id": str(discord_message_id),
            "discord_channel_id": str(discord_channel_id),
            "binding_id": str(binding_id),
            "created_at": to_iso(utc_now()),
            "delivered_receipt_received": False,
            "read_receipt_received": False,
            "has_read_receipt": False,
            "active_reactions": {},
        }

    def get_outbound_message(self, packet_id: str) -> dict[str, Any] | None:
        payload = self.outbound_message_index.get(str(packet_id))
        if payload is None:
            return None
        return dict(payload)

    def find_latest_outbound_message(
        self,
        discord_user_id: str,
        *,
        binding_id: str | None = None,
    ) -> dict[str, Any] | None:
        candidates = [
            dict(payload)
            for payload in self.outbound_message_index.values()
            if payload.get("discord_user_id") == str(discord_user_id)
            and (binding_id is None or payload.get("binding_id") == str(binding_id))
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda item: (parse_datetime(str(item.get("created_at") or "")) or utc_now(), str(item.get("packet_id") or "")))
        return candidates[-1]

    def mark_outbound_receipt(self, packet_id: str, receipt_kind: str) -> None:
        payload = self.outbound_message_index.get(str(packet_id))
        if payload is None:
            return
        if receipt_kind == "read":
            payload["read_receipt_received"] = True
            payload["has_read_receipt"] = True
        elif receipt_kind == "delivered":
            payload["delivered_receipt_received"] = True

    def set_outbound_reaction(self, packet_id: str, reaction_key: str, reaction: dict[str, Any]) -> None:
        payload = self.outbound_message_index.get(str(packet_id))
        if payload is None:
            return
        active_reactions = dict(payload.get("active_reactions") or {})
        active_reactions[str(reaction_key)] = dict(reaction)
        payload["active_reactions"] = active_reactions

    def remove_outbound_reaction(self, packet_id: str, reaction_key: str) -> None:
        payload = self.outbound_message_index.get(str(packet_id))
        if payload is None:
            return
        active_reactions = dict(payload.get("active_reactions") or {})
        active_reactions.pop(str(reaction_key), None)
        payload["active_reactions"] = active_reactions

    def mark_receipt_sent(self, *, binding_id: str, original_packet_id: str, receipt_kind: str) -> None:
        key_name = "read_receipt_sent" if receipt_kind == "read" else "delivered_receipt_sent"
        for payload in self.receipt_index.values():
            if payload.get("binding_id") == str(binding_id) and payload.get("original_packet_id") == str(original_packet_id):
                payload[key_name] = True

    def remove_binding_indexes(self, binding_id: str) -> None:
        self.reply_index = {
            key: value
            for key, value in self.reply_index.items()
            if value.get("binding_id") != str(binding_id)
        }
        self.receipt_index = {
            key: value
            for key, value in self.receipt_index.items()
            if value.get("binding_id") != str(binding_id)
        }
        self.outbound_message_index = {
            key: value
            for key, value in self.outbound_message_index.items()
            if value.get("binding_id") != str(binding_id)
        }
        for user_id, active_binding_id in list(self.active_contacts.items()):
            if active_binding_id == str(binding_id):
                self.active_contacts.pop(user_id, None)
        for user_settings in self.receipt_settings.values():
            contacts = dict(user_settings.get("contacts", {}))
            contacts.pop(str(binding_id), None)
            user_settings["contacts"] = contacts

    def set_receipt_mode(self, discord_user_id: str, mode: str, *, binding_id: str | None = None) -> None:
        normalized_mode = normalize_mode(mode)
        settings = self.receipt_settings.setdefault(
            str(discord_user_id),
            {"default_mode": "off", "contacts": {}},
        )
        if binding_id is None:
            settings["default_mode"] = normalized_mode
        else:
            contacts = dict(settings.get("contacts", {}))
            contacts[str(binding_id)] = normalized_mode
            settings["contacts"] = contacts

    def get_receipt_mode(self, discord_user_id: str, binding_id: str, *, default_mode: str) -> str:
        settings = self.receipt_settings.get(
            str(discord_user_id),
            {"default_mode": str(default_mode), "contacts": {}},
        )
        contacts = dict(settings.get("contacts", {}))
        if str(binding_id) in contacts:
            return normalize_mode(contacts[str(binding_id)])
        return normalize_mode(str(settings.get("default_mode") or default_mode))

    def bindings_using_local_endpoint(self, endpoint_id: str) -> list[Binding]:
        return [binding for binding in self.bindings if binding.local_endpoint_id == str(endpoint_id)]

    def upsert_binding(self, binding: Binding) -> None:
        for index, existing in enumerate(self.bindings):
            if existing.binding_id == binding.binding_id:
                self.bindings[index] = binding
                return
        self.bindings.append(binding)

    def delete_binding(self, binding_id: str) -> Binding | None:
        target = self.get_binding(binding_id)
        if target is None:
            return None
        self.bindings = [binding for binding in self.bindings if binding.binding_id != str(binding_id)]
        self.remove_binding_indexes(binding_id)
        return target

    def pending_for_local_endpoint(self, endpoint_id: str) -> PendingLink | None:
        for pending in self.pending_links:
            if pending.local_endpoint_id == str(endpoint_id):
                return pending
        return None


def should_send_delivered_receipt(mode: str) -> bool:
    return mode in {"discord_delivered", "discord_delivered_and_reaction_read"}


def should_send_read_receipt(mode: str) -> bool:
    return mode in {"reaction_read", "discord_delivered_and_reaction_read"}


def configure_logging(log_path: Path, level_name: str) -> None:
    level = getattr(logging, str(level_name or "INFO").upper(), logging.INFO)
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_path, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=handlers,
        force=True,
    )


def ensure_runtime_config(app: ARQSApp) -> None:
    merged = dict(app.config)
    for key, value in DEFAULT_CONFIG_TEMPLATE.items():
        if merged.get(key) is None:
            merged[key] = value
    merged["app_name"] = APP_NAME
    if str(merged.get("default_endpoint_name") or "").strip() == "":
        merged["default_endpoint_name"] = DEFAULT_CONFIG_TEMPLATE["default_endpoint_name"]
    if str(merged.get("default_endpoint_kind") or "").strip() == "":
        merged["default_endpoint_kind"] = DEFAULT_CONFIG_TEMPLATE["default_endpoint_kind"]
    if str(merged.get("node_name") or "").strip() == "":
        merged["node_name"] = APP_NAME
    if str(merged.get("base_url") or "").strip() == "":
        app.store.save_config(merged)
        raise SystemExit(
            f"ARQS config is missing base_url. Edit {app.store.paths.config_path} and set DISCORD_BOT_TOKEN before running."
        )
    app.setup(
        app_name=APP_NAME,
        base_url=merged["base_url"],
        node_name=merged["node_name"],
        default_endpoint_name=merged["default_endpoint_name"],
        default_endpoint_kind=merged["default_endpoint_kind"],
        transport_policy=merged["transport_policy"],
        delivery_mode=merged["delivery_mode"],
        retry_policy=merged["retry_policy"],
        max_attempts=merged["max_attempts"],
        expires_after_seconds=merged["expires_after_seconds"],
        poll_wait_seconds=merged["poll_wait_seconds"],
        poll_limit=merged["poll_limit"],
        discord_sync_commands_on_start=merged["discord_sync_commands_on_start"],
        discord_log_level=merged["discord_log_level"],
        receipt_default_mode=merged["receipt_default_mode"],
    )


if discord is not None:  # pragma: no branch
    class DeleteLinkView(discord.ui.View):
        def __init__(
            self,
            *,
            bot: "ARQSDiscordBot",
            binding_id: str,
            binding_label: str,
            discord_user_id: str,
            require_current_on_confirm: bool,
        ) -> None:
            super().__init__(timeout=60)
            self.bot = bot
            self.binding_id = str(binding_id)
            self.binding_label = str(binding_label)
            self.discord_user_id = str(discord_user_id)
            self.require_current_on_confirm = bool(require_current_on_confirm)
            self.message: discord.Message | None = None

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if str(interaction.user.id) != self.discord_user_id:
                await self.bot.respond(interaction, "Only the requesting user can confirm this deletion.")
                return False
            return True

        async def on_timeout(self) -> None:
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                with contextlib.suppress(discord.HTTPException):
                    await self.message.edit(view=self)

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(content="Link deletion cancelled.", view=self)

        @discord.ui.button(label="Delete link", style=discord.ButtonStyle.danger)
        async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
            await self.bot.confirm_delete_link(interaction, binding_id=self.binding_id, expected_user_id=self.discord_user_id, view=self)
else:
    class DeleteLinkView:  # pragma: no cover - runtime dependency fallback
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("discord.py is required to use DeleteLinkView")


if commands is not None:  # pragma: no branch
    class ARQSDiscordBot(commands.Bot):
        def __init__(self, app: ARQSApp, state: DiscordBridgeState, *, sync_commands_on_start: bool = False) -> None:
            intents = discord.Intents.default()
            intents.messages = True
            intents.dm_messages = True
            intents.reactions = True
            intents.message_content = False
            super().__init__(command_prefix=commands.when_mentioned, intents=intents)
            self.app = app
            self.state = state
            self.sync_commands_on_start = bool(sync_commands_on_start)
            self.polling_task: asyncio.Task[None] | None = None
            self.reconcile_task: asyncio.Task[None] | None = None
            self._commands_registered = False

        async def setup_hook(self) -> None:
            self.register_app_commands()
            if self.sync_commands_on_start:
                await self.tree.sync()
                logger.info("Synced Discord slash commands.")

        async def on_ready(self) -> None:
            logger.info("Discord bot ready as %s", self.user)
            if self.polling_task is None or self.polling_task.done():
                self.polling_task = asyncio.create_task(self.poll_inbox_loop(), name="arqs-discord-poll")
            if self.reconcile_task is None or self.reconcile_task.done():
                self.reconcile_task = asyncio.create_task(self.reconcile_loop(), name="arqs-discord-reconcile")
            if str(self.app.config.get("delivery_mode") or "queued") == "background":
                self.app.start_outbox_thread()

        async def close(self) -> None:
            for task in (self.polling_task, self.reconcile_task):
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
            self.app.stop_receiver_thread()
            self.app.stop_outbox_thread()
            self.state.save()
            await super().close()

        def register_app_commands(self) -> None:
            if self._commands_registered:
                return

            @app_commands.command(name="request_link_code", description="Create an ARQS link code for a new DM contact.")
            @app_commands.describe(mode="Choose bidirectional, send_only, or receive_only")
            async def request_link_code(
                interaction: discord.Interaction,
                mode: Literal["bidirectional", "send_only", "receive_only"] = "bidirectional",
            ) -> None:
                await self.cmd_request_link_code(interaction, mode)

            @app_commands.command(name="redeem_link_code", description="Redeem an ARQS link code.")
            @app_commands.describe(code="The link code shared with you by another ARQS user")
            async def redeem_link_code(interaction: discord.Interaction, code: str) -> None:
                await self.cmd_redeem_link_code(interaction, code)

            @app_commands.command(name="links", description="List your linked ARQS contacts.")
            async def links(interaction: discord.Interaction) -> None:
                await self.cmd_links(interaction)

            @app_commands.command(name="use_contact", description="Choose the active contact for normal messages.")
            @app_commands.describe(contact="A contact number from /links or a contact label")
            async def use_contact(interaction: discord.Interaction, contact: str) -> None:
                await self.cmd_use_contact(interaction, contact)

            @app_commands.command(name="current_contact", description="Show the current contact and its capabilities.")
            async def current_contact(interaction: discord.Interaction) -> None:
                await self.cmd_current_contact(interaction)

            @app_commands.command(name="rename_contact", description="Rename the active contact.")
            @app_commands.describe(new_name="The new local name for the active contact")
            async def rename_contact(interaction: discord.Interaction, new_name: str) -> None:
                await self.cmd_rename_contact(interaction, new_name)

            @app_commands.command(name="delete_link", description="Delete an ARQS contact link.")
            @app_commands.describe(contact="Optional contact label or number; defaults to the current contact")
            async def delete_link(interaction: discord.Interaction, contact: str | None = None) -> None:
                await self.cmd_delete_link(interaction, contact)

            @app_commands.command(name="command", description="Send a fire-and-forget command.v1 packet.")
            @app_commands.describe(text="The raw command text", contact="Optional contact label or number")
            async def command(interaction: discord.Interaction, text: str, contact: str | None = None) -> None:
                await self.cmd_command(interaction, text, contact)

            receipts_group = app_commands.Group(name="receipts", description="Manage ARQS receipt behavior.")

            @receipts_group.command(name="status", description="Show your current receipt settings.")
            async def receipts_status(interaction: discord.Interaction) -> None:
                await self.cmd_receipts_status(interaction)

            @receipts_group.command(name="off", description="Disable ARQS client receipts.")
            @app_commands.describe(contact="Optional contact label or number")
            async def receipts_off(interaction: discord.Interaction, contact: str | None = None) -> None:
                await self.cmd_set_receipts(interaction, "off", contact)

            @receipts_group.command(name="discord_delivered", description="Send receipt.received.v1 after Discord delivery.")
            @app_commands.describe(contact="Optional contact label or number")
            async def receipts_delivered(interaction: discord.Interaction, contact: str | None = None) -> None:
                await self.cmd_set_receipts(interaction, "discord_delivered", contact)

            @receipts_group.command(name="reaction_read", description="Send receipt.read.v1 after a user reaction.")
            @app_commands.describe(contact="Optional contact label or number")
            async def receipts_reaction_read(interaction: discord.Interaction, contact: str | None = None) -> None:
                await self.cmd_set_receipts(interaction, "reaction_read", contact)

            @receipts_group.command(
                name="delivered_and_read",
                description="Enable both Discord-delivered and reaction-based read receipts.",
            )
            @app_commands.describe(contact="Optional contact label or number")
            async def receipts_both(interaction: discord.Interaction, contact: str | None = None) -> None:
                await self.cmd_set_receipts(interaction, "discord_delivered_and_reaction_read", contact)

            @app_commands.command(name="status", description="Show the bot and AppKit runtime status.")
            async def status(interaction: discord.Interaction) -> None:
                await self.cmd_status(interaction)

            @app_commands.command(name="flush_outbox", description="Flush the AppKit outbox now.")
            async def flush_outbox(interaction: discord.Interaction) -> None:
                await self.cmd_flush_outbox(interaction)

            for command_obj in (
                request_link_code,
                redeem_link_code,
                links,
                use_contact,
                current_contact,
                rename_contact,
                delete_link,
                command,
                status,
                flush_outbox,
                receipts_group,
            ):
                self.tree.add_command(command_obj)
            self._commands_registered = True

        async def respond(self, interaction: discord.Interaction, content: str, *, view: discord.ui.View | None = None) -> None:
            if interaction.response.is_done():
                if view is None:
                    await interaction.followup.send(content)
                else:
                    await interaction.followup.send(content, view=view)
            else:
                if view is None:
                    await interaction.response.send_message(content)
                else:
                    await interaction.response.send_message(content, view=view)

        async def ensure_dm_interaction(self, interaction: discord.Interaction) -> bool:
            if interaction.guild is None:
                return True
            await self.respond(interaction, "This command only works in direct messages.")
            return False

        async def send_user_dm(self, discord_user_id: str, content: str) -> list[discord.Message]:
            user = self.get_user(int(discord_user_id))
            if user is None:
                user = await self.fetch_user(int(discord_user_id))
            chunks = split_discord_message(content)
            if not chunks:
                return []
            sent: list[discord.Message] = []
            for chunk in chunks:
                sent.append(await user.send(chunk))
            return sent

        def resolve_binding_selector(self, discord_user_id: str, selector: str) -> Binding | None:
            bindings = self.state.list_user_bindings(discord_user_id)
            if not bindings:
                return None
            raw = str(selector or "").strip()
            if not raw:
                return None
            if raw.isdigit():
                index = int(raw) - 1
                if 0 <= index < len(bindings):
                    return bindings[index]
            lowered = raw.casefold()
            exact = [binding for binding in bindings if binding.label.casefold() == lowered]
            if len(exact) == 1:
                return exact[0]
            prefix = [binding for binding in bindings if binding.label.casefold().startswith(lowered)]
            if len(prefix) == 1:
                return prefix[0]
            return None

        def resolve_default_binding(self, discord_user_id: str) -> Binding:
            bindings = self.state.list_user_bindings(discord_user_id)
            if not bindings:
                raise ValueError("You do not have any linked ARQS contacts yet.")
            active = self.state.ensure_valid_active_binding(discord_user_id)
            if len(bindings) == 1:
                return bindings[0]
            if active is not None:
                return active
            raise ValueError(
                "You have multiple linked contacts. Use /use_contact first, or reply directly to one of my forwarded messages."
            )

        def resolve_reply_binding(self, message: discord.Message) -> Binding | None:
            reference = message.reference
            if reference is None or reference.message_id is None:
                return None
            entry = self.state.reply_index.get(str(reference.message_id))
            if entry is None:
                return None
            if entry.get("discord_user_id") != str(message.author.id):
                return None
            binding = self.state.get_binding(str(entry.get("binding_id") or ""))
            if binding is None or binding.status != "active":
                return None
            return binding

        async def on_message(self, message: discord.Message) -> None:
            if message.author.bot:
                return
            if message.guild is not None:
                return
            content = str(message.content or "").strip()
            if not content:
                return
            try:
                binding = self.resolve_reply_binding(message) or self.resolve_default_binding(str(message.author.id))
            except ValueError as exc:
                await message.channel.send(str(exc))
                return
            if not binding.can_send:
                await message.channel.send(
                    "This contact is receive-only from Discord. You can receive messages from it, but you cannot send messages back over this ARQS link."
                )
                return
            try:
                result = await asyncio.to_thread(
                    self.app.send_type,
                    arqs_type=TYPE_MESSAGE_V1,
                    body=content,
                    from_endpoint_id=binding.local_endpoint_id,
                    to_endpoint_id=binding.remote_endpoint_id,
                    content_type="text/plain; charset=utf-8",
                    meta={
                        "adapter": "discord_dm",
                        "discord_user_id": str(message.author.id),
                        "discord_user": user_display_name(message.author),
                        "discord_message_id": str(message.id),
                        "discord_reply_to_message_id": None if message.reference is None or message.reference.message_id is None else str(message.reference.message_id),
                    },
                )
            except Exception as exc:
                logger.exception("failed to send outbound message for Discord user %s", message.author.id)
                await message.channel.send(f"Message send failed: {exc}")
                return
            if send_result_label(result.status) not in {"sent", "queued"}:
                await message.channel.send(f"Message send failed: {result.status}")
                return
            binding.updated_at = to_iso(utc_now())
            self.state.upsert_binding(binding)
            self.state.set_active_binding(str(message.author.id), binding.binding_id)
            self.state.remember_outbound_message(
                packet_id=result.packet_id,
                discord_user_id=str(message.author.id),
                discord_message_id=str(message.id),
                discord_channel_id=str(message.channel.id),
                binding_id=binding.binding_id,
            )
            self.state.save()

        async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
            if self.user is not None and payload.user_id == self.user.id:
                return
            entry = self.state.receipt_index.get(str(payload.message_id))
            if entry is None:
                return
            if str(entry.get("discord_user_id")) != str(payload.user_id):
                return
            binding = self.state.get_binding(str(entry.get("binding_id") or ""))
            if binding is None or binding.status != "active" or not binding.can_send:
                return
            try:
                await self.send_reaction_from_discord_event(binding, entry, payload, action="set")
            except Exception:
                reactions_logger.exception("failed to send reaction packet for Discord message %s", payload.message_id)
                return
            if bool(entry.get("read_receipt_sent")):
                self.state.save()
                return
            try:
                await self.send_read_receipt(binding, entry, discord_user_id=str(payload.user_id), discord_message_id=str(payload.message_id))
            except Exception:
                receipts_logger.exception("failed to send read receipt for Discord message %s", payload.message_id)
                return
            self.state.mark_receipt_sent(binding_id=binding.binding_id, original_packet_id=str(entry["original_packet_id"]), receipt_kind="read")
            self.state.save()

        async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
            if self.user is not None and payload.user_id == self.user.id:
                return
            entry = self.state.receipt_index.get(str(payload.message_id))
            if entry is None:
                return
            if str(entry.get("discord_user_id")) != str(payload.user_id):
                return
            binding = self.state.get_binding(str(entry.get("binding_id") or ""))
            if binding is None or binding.status != "active" or not binding.can_send:
                return
            try:
                await self.send_reaction_from_discord_event(binding, entry, payload, action="remove")
            except Exception:
                reactions_logger.exception("failed to send reaction-remove packet for Discord message %s", payload.message_id)
                return
            self.state.save()

        async def cmd_request_link_code(self, interaction: discord.Interaction, mode: str) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            await interaction.response.defer(thinking=True)
            user_id = str(interaction.user.id)
            label = self.state.next_contact_label(user_id)
            endpoint: Endpoint | None = None
            try:
                client = self.app.require_client()
                endpoint = await asyncio.to_thread(
                    client.create_endpoint,
                    endpoint_name=make_hidden_endpoint_name(user_id),
                    kind="discord_dm",
                    meta={"discord_user_id": user_id, "scope": "dm"},
                )
                link_code = await asyncio.to_thread(
                    client.request_link_code,
                    str(endpoint.endpoint_id),
                    requested_mode=user_mode_to_arqs_mode(mode),
                )
            except Exception as exc:
                if endpoint is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.app.require_client().delete_endpoint, str(endpoint.endpoint_id))
                await interaction.followup.send(f"Request link code failed: {exc}")
                return
            pending = PendingLink(
                pending_id=str(uuid.uuid4()),
                discord_user_id=user_id,
                local_endpoint_id=str(endpoint.endpoint_id),
                code=str(link_code.code),
                requested_mode=str(mode),
                label=label,
                created_at=to_iso(utc_now()),
                expires_at=to_iso(link_code.expires_at),
            )
            self.state.add_pending_link(pending)
            self.state.save()
            expires_at = link_code.expires_at.astimezone()
            remaining_seconds = max(0, int((link_code.expires_at - utc_now()).total_seconds()))
            remaining_minutes = max(1, (remaining_seconds + 59) // 60)
            await interaction.followup.send(
                f"Link code created for {label}.\n\n"
                f"Code: {link_code.code}\n"
                f"Mode: {mode}\n"
                f"Expires in {remaining_minutes} minute{'s' if remaining_minutes != 1 else ''} at {expires_at.strftime('%I:%M %p %Z')}\n\n"
                "Share this code with the other ARQS user. When the link becomes active, I will DM you."
            )

        async def cmd_redeem_link_code(self, interaction: discord.Interaction, code: str) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            await interaction.response.defer(thinking=True)
            user_id = str(interaction.user.id)
            label = self.state.next_contact_label(user_id)
            endpoint: Endpoint | None = None
            try:
                client = self.app.require_client()
                endpoint = await asyncio.to_thread(
                    client.create_endpoint,
                    endpoint_name=make_hidden_endpoint_name(user_id),
                    kind="discord_dm",
                    meta={"discord_user_id": user_id, "scope": "dm"},
                )
                link = await asyncio.to_thread(client.redeem_link_code, code, str(endpoint.endpoint_id))
            except Exception as exc:
                if endpoint is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self.app.require_client().delete_endpoint, str(endpoint.endpoint_id))
                await interaction.followup.send(f"Link code redeem failed: {exc}")
                return
            can_send, can_receive = calculate_direction(link, str(endpoint.endpoint_id))
            binding = Binding(
                binding_id=str(uuid.uuid4()),
                discord_user_id=user_id,
                local_endpoint_id=str(endpoint.endpoint_id),
                remote_endpoint_id=resolve_remote_endpoint(link, str(endpoint.endpoint_id)),
                link_id=str(link.link_id),
                label=label,
                link_mode=str(link.mode),
                can_send=can_send,
                can_receive=can_receive,
                status=str(link.status or "active"),
                created_at=to_iso(utc_now()),
                updated_at=to_iso(utc_now()),
            )
            self.state.upsert_binding(binding)
            if self.state.get_active_binding(user_id) is None:
                self.state.set_active_binding(user_id, binding.binding_id)
            self.state.save()
            await interaction.followup.send(
                f"Link redeemed successfully as {binding.label}.\n"
                f"Direction: {binding_direction(binding)}\n"
                f"Can send: {'yes' if binding.can_send else 'no'}\n"
                f"Can receive: {'yes' if binding.can_receive else 'no'}"
            )

        async def cmd_links(self, interaction: discord.Interaction) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            bindings = self.state.list_user_bindings(str(interaction.user.id))
            if not bindings:
                await self.respond(interaction, "You do not have any linked ARQS contacts yet.")
                return
            active = self.state.ensure_valid_active_binding(str(interaction.user.id))
            lines = ["Linked contacts:"]
            for index, binding in enumerate(bindings, start=1):
                marker = "*" if active is not None and active.binding_id == binding.binding_id else "-"
                suffix = "" if binding.status == "active" else f" ({binding.status})"
                lines.append(f"{marker} [{index}] {binding.label} — {binding_direction(binding)}{suffix}")
            await self.respond(interaction, "```text\n" + "\n".join(lines) + "\n```")

        async def cmd_use_contact(self, interaction: discord.Interaction, contact: str) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            binding = self.resolve_binding_selector(str(interaction.user.id), contact)
            if binding is None:
                await self.respond(interaction, "Contact not found.")
                return
            self.state.set_active_binding(str(interaction.user.id), binding.binding_id)
            self.state.save()
            await self.respond(interaction, f"Active contact set to {binding.label}.")

        async def cmd_current_contact(self, interaction: discord.Interaction) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            try:
                binding = self.resolve_default_binding(str(interaction.user.id))
            except ValueError:
                await self.respond(interaction, "No active contact is selected.")
                return
            receipt_mode = self.state.get_receipt_mode(
                str(interaction.user.id),
                binding.binding_id,
                default_mode=str(self.app.config.get("receipt_default_mode") or "off"),
            )
            lines = [
                f"Current active contact: {binding.label}",
                f"Direction: {binding_direction(binding)}",
                f"Can send: {'yes' if binding.can_send else 'no'}",
                f"Can receive: {'yes' if binding.can_receive else 'no'}",
                f"Receipts: {receipt_mode}",
            ]
            if receipt_mode != "off" and not binding.can_send:
                lines.append(
                    "Receipts are configured, but this contact has no reverse send route. Receipts will not be sent unless the link direction changes."
                )
            await self.respond(interaction, "\n".join(lines))

        async def cmd_rename_contact(self, interaction: discord.Interaction, new_name: str) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            active = self.state.ensure_valid_active_binding(str(interaction.user.id))
            if active is None:
                await self.respond(interaction, "No active contact is selected.")
                return
            candidate = str(new_name or "").strip()
            if not candidate:
                await self.respond(interaction, "Contact name cannot be empty.")
                return
            unique_name = self.state.ensure_unique_label(
                str(interaction.user.id),
                candidate,
                exclude_binding_id=active.binding_id,
                include_inactive=False,
            )
            if unique_name != candidate:
                await self.respond(interaction, "That contact name is already in use.")
                return
            active.label = candidate
            active.updated_at = to_iso(utc_now())
            self.state.upsert_binding(active)
            self.state.save()
            await self.respond(interaction, f"Active contact renamed to {candidate}.")

        async def cmd_delete_link(self, interaction: discord.Interaction, contact: str | None) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            if contact:
                binding = self.resolve_binding_selector(str(interaction.user.id), contact)
                if binding is None:
                    await self.respond(interaction, "Contact not found.")
                    return
            else:
                binding = self.state.ensure_valid_active_binding(str(interaction.user.id))
                if binding is None:
                    await self.respond(interaction, "No active contact is selected.")
                    return
            view = DeleteLinkView(
                bot=self,
                binding_id=binding.binding_id,
                binding_label=binding.label,
                discord_user_id=str(interaction.user.id),
                require_current_on_confirm=not bool(contact),
            )
            warning = (
                f"This will delete the ARQS link for contact {binding.label}. This cannot be undone. "
                "Messages will stop until you create a new link."
            )
            await self.respond(interaction, warning, view=view)
            with contextlib.suppress(discord.HTTPException):
                view.message = await interaction.original_response()

        async def confirm_delete_link(
            self,
            interaction: discord.Interaction,
            *,
            binding_id: str,
            expected_user_id: str,
            view: DeleteLinkView,
        ) -> None:
            binding = self.state.get_binding(binding_id)
            current_active = self.state.ensure_valid_active_binding(expected_user_id)
            if binding is None or binding.discord_user_id != expected_user_id:
                for child in view.children:
                    child.disabled = True
                await interaction.response.edit_message(content="The link is no longer available.", view=view)
                return
            if view.require_current_on_confirm and (current_active is None or current_active.binding_id != binding_id):
                for child in view.children:
                    child.disabled = True
                await interaction.response.edit_message(content="The active contact changed before deletion was confirmed.", view=view)
                return
            details: list[str] = []
            client = self.app.require_client()
            if binding.link_id:
                try:
                    await asyncio.to_thread(client.revoke_link, binding.link_id)
                    details.append("Server-side link revoked.")
                except Exception as exc:
                    details.append(f"Server-side revoke failed: {exc}")
            if len(self.state.bindings_using_local_endpoint(binding.local_endpoint_id)) == 1 and self.state.pending_for_local_endpoint(binding.local_endpoint_id) is None:
                try:
                    await asyncio.to_thread(client.delete_endpoint, binding.local_endpoint_id)
                    details.append("Hidden local endpoint deleted.")
                except Exception as exc:
                    details.append(f"Local endpoint delete failed: {exc}")
            self.state.delete_binding(binding_id)
            remaining = self.state.list_user_bindings(expected_user_id)
            if len(remaining) == 1:
                self.state.set_active_binding(expected_user_id, remaining[0].binding_id)
            elif not remaining:
                self.state.set_active_binding(expected_user_id, None)
            self.state.save()
            for child in view.children:
                child.disabled = True
            summary = f"Link deleted for {view.binding_label}."
            if details:
                summary += "\n" + "\n".join(details)
            await interaction.response.edit_message(content=summary, view=view)

        async def cmd_command(self, interaction: discord.Interaction, text: str, contact: str | None) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            raw_text = str(text or "").strip()
            if not raw_text:
                await self.respond(interaction, "Command text cannot be empty.")
                return
            try:
                binding = (
                    self.resolve_binding_selector(str(interaction.user.id), contact)
                    if contact
                    else self.resolve_default_binding(str(interaction.user.id))
                )
                if binding is None:
                    raise ValueError("Contact not found.")
            except ValueError as exc:
                await self.respond(interaction, str(exc))
                return
            if not binding.can_send:
                await self.respond(
                    interaction,
                    "This contact is receive-only from Discord. You can receive messages from it, but you cannot send messages back over this ARQS link.",
                )
                return
            await interaction.response.defer(thinking=True)
            try:
                result = await asyncio.to_thread(
                    self.app.send_type,
                    arqs_type=TYPE_COMMAND_V1,
                    body=raw_text,
                    data={
                        "command_id": str(uuid.uuid4()),
                        "command": raw_text,
                        "args": {"raw": raw_text},
                        "created_at": to_iso(utc_now()),
                    },
                    from_endpoint_id=binding.local_endpoint_id,
                    to_endpoint_id=binding.remote_endpoint_id,
                    correlation_id=str(uuid.uuid4()),
                    meta={
                        "adapter": "discord_dm",
                        "discord_user_id": str(interaction.user.id),
                        "discord_user": user_display_name(interaction.user),
                        "discord_interaction_id": str(interaction.id),
                    },
                )
            except Exception as exc:
                await interaction.followup.send(f"Command send failed: {exc}")
                return
            self.state.set_active_binding(str(interaction.user.id), binding.binding_id)
            binding.updated_at = to_iso(utc_now())
            self.state.upsert_binding(binding)
            self.state.save()
            await interaction.followup.send(f"Command {send_result_label(result.status)} for {binding.label}.")

        async def cmd_receipts_status(self, interaction: discord.Interaction) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            user_id = str(interaction.user.id)
            settings = self.state.receipt_settings.get(user_id, {"default_mode": str(self.app.config.get("receipt_default_mode") or "off"), "contacts": {}})
            lines = [f"Receipt settings:\nDefault: {settings.get('default_mode', 'off')}"]
            contacts = dict(settings.get("contacts", {}))
            if contacts:
                lines.append("\nContact overrides:")
                bindings = {binding.binding_id: binding for binding in self.state.list_user_bindings(user_id, include_inactive=True)}
                shown = 0
                for binding in self.state.list_user_bindings(user_id, include_inactive=True):
                    if binding.binding_id not in contacts:
                        continue
                    shown += 1
                    lines.append(f"{shown}. {binding.label} — {contacts[binding.binding_id]}")
            await self.respond(interaction, "\n".join(lines))

        async def cmd_set_receipts(self, interaction: discord.Interaction, mode: str, contact: str | None) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            user_id = str(interaction.user.id)
            if contact:
                binding = self.resolve_binding_selector(user_id, contact)
                if binding is None:
                    await self.respond(interaction, "Contact not found.")
                    return
                self.state.set_receipt_mode(user_id, mode, binding_id=binding.binding_id)
                self.state.save()
                message = f"Receipt mode for {binding.label} set to {mode}."
                if mode != "off" and not binding.can_send:
                    message += (
                        "\nReceipts are configured, but this contact has no reverse send route. "
                        "Receipts will not be sent unless the link direction changes."
                    )
                await self.respond(interaction, message)
                return
            self.state.set_receipt_mode(user_id, mode)
            self.state.save()
            await self.respond(interaction, f"Default receipt mode set to {mode}.")

        async def cmd_status(self, interaction: discord.Interaction) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            base_url = str(self.app.require_client().base_url)
            bindings = len([binding for binding in self.state.bindings if binding.status == "active"])
            pending = len(self.state.pending_links)
            lines = [
                "ARQS Discord Bot Status",
                f"App: {APP_NAME}",
                f"ARQS base URL: {base_url}",
                f"Node loaded: {'yes' if self.app.identity is not None else 'no'}",
                f"Bindings: {bindings}",
                f"Pending links: {pending}",
                f"Poll wait: {int(self.app.config.get('poll_wait_seconds', 20))} seconds",
                f"Outbox: {self.app.config.get('delivery_mode', 'queued')}",
            ]
            await self.respond(interaction, "\n".join(lines))

        async def cmd_flush_outbox(self, interaction: discord.Interaction) -> None:
            if not await self.ensure_dm_interaction(interaction):
                return
            await interaction.response.defer(thinking=True)
            try:
                results = await asyncio.to_thread(self.app.flush_outbox)
            except Exception as exc:
                await interaction.followup.send(f"Outbox flush failed: {exc}")
                return
            if not results:
                await interaction.followup.send("No queued outbox packets were due for flushing.")
                return
            counts: dict[str, int] = {}
            for result in results:
                counts[result.status] = counts.get(result.status, 0) + 1
            lines = ["Outbox flush complete:"]
            for status, count in sorted(counts.items()):
                lines.append(f"- {status}: {count}")
            await interaction.followup.send("\n".join(lines))

        async def poll_inbox_loop(self) -> None:
            while True:
                try:
                    await self.poll_inbox_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("inbox poll loop failed")
                    await asyncio.sleep(2)

        async def poll_inbox_once(self) -> None:
            wait_seconds = int(self.app.config.get("poll_wait_seconds", 20))
            poll_limit = int(self.app.config.get("poll_limit", 100))
            client = self.app.require_client()
            deliveries = await asyncio.to_thread(
                client.poll_inbox,
                wait=wait_seconds,
                limit=poll_limit,
                request_timeout=wait_seconds + 10,
            )
            for delivery in deliveries:
                await self.handle_delivery(delivery)

        async def handle_delivery(self, delivery: Delivery) -> None:
            delivery_id = str(delivery.delivery_id)
            if self.state.has_seen_delivery(delivery_id):
                await asyncio.to_thread(self.app.require_client().ack_delivery, delivery_id, status="handled")
                return
            packet = delivery.packet
            binding = self.state.get_binding_by_local_endpoint(str(packet.to_endpoint_id))
            if binding is None:
                binding = await self.materialize_pending_binding(str(packet.to_endpoint_id))
            if binding is None:
                if self.state.pending_for_local_endpoint(str(packet.to_endpoint_id)) is not None:
                    links_logger.warning(
                        "delivery arrived for pending local endpoint %s before activation materialized; leaving it unacked for retry",
                        packet.to_endpoint_id,
                    )
                    return
                links_logger.warning("acknowledging delivery for unknown local endpoint %s", packet.to_endpoint_id)
                await asyncio.to_thread(self.app.require_client().ack_delivery, delivery_id, status="handled")
                return
            if not binding.can_receive:
                links_logger.warning(
                    "received packet for binding %s even though local cache says can_receive=false; forwarding anyway",
                    binding.binding_id,
                )
            if await self.handle_reaction_delivery(binding, delivery):
                self.state.set_active_binding(binding.discord_user_id, binding.binding_id)
                binding.updated_at = to_iso(utc_now())
                self.state.upsert_binding(binding)
                self.state.mark_seen_delivery(delivery_id)
                self.state.save()
                await asyncio.to_thread(self.app.require_client().ack_delivery, delivery_id, status="handled")
                return
            if await self.handle_receipt_delivery(binding, delivery):
                self.state.set_active_binding(binding.discord_user_id, binding.binding_id)
                binding.updated_at = to_iso(utc_now())
                self.state.upsert_binding(binding)
                self.state.mark_seen_delivery(delivery_id)
                self.state.save()
                await asyncio.to_thread(self.app.require_client().ack_delivery, delivery_id, status="handled")
                return
            rendered = self.render_inbound_packet(binding, delivery)
            sent_messages = await self.send_user_dm(binding.discord_user_id, rendered)
            message_ids = [message.id for message in sent_messages]
            self.state.remember_reply_messages(
                discord_user_id=binding.discord_user_id,
                binding_id=binding.binding_id,
                packet_id=str(packet.packet_id),
                message_ids=message_ids,
            )
            receipt_mode = self.state.get_receipt_mode(
                binding.discord_user_id,
                binding.binding_id,
                default_mode=str(self.app.config.get("receipt_default_mode") or "off"),
            )
            packet_type = self.packet_type_for_delivery(delivery)
            receipt_eligible = not is_receipt_type(packet_type) and packet_type != TYPE_REACTION_V1 and binding.can_send
            if receipt_eligible:
                self.state.remember_receipt_messages(
                    discord_user_id=binding.discord_user_id,
                    binding_id=binding.binding_id,
                    original_packet_id=str(packet.packet_id),
                    original_from_endpoint_id=str(packet.from_endpoint_id),
                    original_to_endpoint_id=str(packet.to_endpoint_id),
                    original_correlation_id=get_correlation_id(packet.headers),
                    message_ids=message_ids,
                )
            if receipt_eligible and receipt_mode != "off" and should_send_delivered_receipt(receipt_mode):
                try:
                    await self.send_delivered_receipt(binding, delivery)
                except Exception:
                    receipts_logger.exception("failed to send delivered receipt for packet %s", packet.packet_id)
                else:
                    self.state.mark_receipt_sent(
                        binding_id=binding.binding_id,
                        original_packet_id=str(packet.packet_id),
                        receipt_kind="delivered",
                    )
            self.state.set_active_binding(binding.discord_user_id, binding.binding_id)
            binding.updated_at = to_iso(utc_now())
            self.state.upsert_binding(binding)
            self.state.mark_seen_delivery(delivery_id)
            self.state.save()
            await asyncio.to_thread(self.app.require_client().ack_delivery, delivery_id, status="handled")

        async def handle_reaction_delivery(self, binding: Binding, delivery: Delivery) -> bool:
            packet_type = self.packet_type_for_delivery(delivery)
            if packet_type != TYPE_REACTION_V1:
                return False
            packet = delivery.packet
            data = dict(packet.data or {})
            action = str(data.get("action") or "").strip().lower()
            if action not in {"set", "remove"}:
                return False
            for_packet_id = str(data.get("for_packet_id") or get_causation_id(packet.headers) or "").strip()
            target = self.find_outbound_message_target(binding, for_packet_id)
            if target is None:
                reactions_logger.warning(
                    "received reaction for packet %s but could not map it to a Discord message for user %s",
                    for_packet_id or "<missing>",
                    binding.discord_user_id,
                )
                return False
            reaction_key = get_reaction_key(data)
            if reaction_key is None:
                return False
            reaction_state = self.reaction_state_from_packet(data)
            if not reaction_display_key(reaction_state):
                return False
            reaction_state["reaction_key"] = reaction_key
            active_reactions = self.active_reactions_for_target(target)
            if action == "set":
                previous_reaction = active_reactions.get(reaction_key)
                had_explicit_reactions = bool(active_reactions)
                active_reactions[reaction_key] = reaction_state
                self.state.set_outbound_reaction(str(target["packet_id"]), reaction_key, reaction_state)
                target["active_reactions"] = active_reactions
                await self.try_set_explicit_reaction(
                    target,
                    reaction_state,
                    previous_reaction=previous_reaction,
                    suppress_read_marker=not had_explicit_reactions,
                )
                return True

            stored_reaction = active_reactions.pop(reaction_key, None)
            removed_reaction = stored_reaction or reaction_state
            if not any(reaction_matches(remaining, removed_reaction) for remaining in active_reactions.values()):
                await self.try_remove_display_reaction(target, removed_reaction)
            self.state.remove_outbound_reaction(str(target["packet_id"]), reaction_key)
            target["active_reactions"] = active_reactions
            if not active_reactions and self.target_has_read_receipt(target):
                await self.try_add_receipt_reaction(target)
            return True

        async def handle_receipt_delivery(self, binding: Binding, delivery: Delivery) -> bool:
            packet_type = self.packet_type_for_delivery(delivery)
            if packet_type not in {TYPE_RECEIPT_READ_V1, TYPE_RECEIPT_RECEIVED_V1}:
                return False
            packet = delivery.packet
            for_packet_id = str(packet.data.get("for_packet_id") or "").strip() if isinstance(packet.data, dict) else ""
            if not for_packet_id:
                for_packet_id = str(get_causation_id(packet.headers) or "").strip()
            exact_target = None if not for_packet_id else self.state.get_outbound_message(for_packet_id)
            if packet_type == TYPE_RECEIPT_RECEIVED_V1 and exact_target is not None:
                self.state.mark_outbound_receipt(exact_target["packet_id"], "delivered")
                return True
            if packet_type != TYPE_RECEIPT_READ_V1:
                return True
            if exact_target is not None:
                self.state.mark_outbound_receipt(exact_target["packet_id"], "read")
                exact_target["read_receipt_received"] = True
                exact_target["has_read_receipt"] = True
                await self.try_add_receipt_reaction(exact_target)
                return True
            fallback_target = self.find_outbound_message_target(binding, "")
            if fallback_target is not None and await self.try_add_receipt_reaction(fallback_target):
                self.state.mark_outbound_receipt(fallback_target["packet_id"], "read")
                fallback_target["read_receipt_received"] = True
                fallback_target["has_read_receipt"] = True
                return True
            receipts_logger.warning(
                "received read receipt for packet %s but could not map it to a Discord message for user %s",
                for_packet_id or "<missing>",
                binding.discord_user_id,
            )
            return True

        def find_outbound_message_target(self, binding: Binding, for_packet_id: str) -> dict[str, Any] | None:
            exact_target = None if not for_packet_id else self.state.get_outbound_message(for_packet_id)
            if exact_target is not None:
                return exact_target
            return self.state.find_latest_outbound_message(
                binding.discord_user_id,
                binding_id=binding.binding_id,
            ) or self.state.find_latest_outbound_message(binding.discord_user_id)

        def reaction_state_from_packet(self, data: dict[str, Any]) -> dict[str, Any]:
            payload: dict[str, Any] = {
                "emoji_name": str(data.get("emoji_name") or data.get("emoji") or "").strip(),
            }
            if data.get("emoji") not in (None, ""):
                payload["emoji"] = str(data["emoji"])
            if data.get("emoji_id") not in (None, ""):
                payload["emoji_id"] = str(data["emoji_id"])
            if data.get("animated") is not None:
                payload["animated"] = bool(data.get("animated"))
            return payload

        def active_reactions_for_target(self, target: dict[str, Any]) -> dict[str, dict[str, Any]]:
            return {
                str(key): dict(value)
                for key, value in dict(target.get("active_reactions") or {}).items()
                if isinstance(value, dict)
            }

        def target_has_read_receipt(self, target: dict[str, Any]) -> bool:
            return bool(target.get("has_read_receipt") or target.get("read_receipt_received"))

        def discord_emoji_from_reaction(self, reaction: dict[str, Any] | None) -> Any | None:
            payload = dict(reaction or {})
            emoji = str(payload.get("emoji") or "").strip()
            if emoji:
                return emoji
            emoji_id = str(payload.get("emoji_id") or "").strip()
            emoji_name = str(payload.get("emoji_name") or "").strip().strip(":")
            if emoji_id:
                try:
                    return discord.PartialEmoji(
                        name=emoji_name or "reaction",
                        id=int(emoji_id),
                        animated=bool(payload.get("animated")),
                    )
                except ValueError:
                    return None
            if emoji_name and not emoji_name.replace("_", "").isalnum():
                return emoji_name
            return None

        async def fetch_target_discord_message(self, target: dict[str, Any]) -> discord.Message | None:
            discord_user_id = str(target.get("discord_user_id") or "")
            discord_message_id = str(target.get("discord_message_id") or "")
            if not discord_user_id or not discord_message_id:
                return None
            user = self.get_user(int(discord_user_id))
            if user is None:
                user = await self.fetch_user(int(discord_user_id))
            channel = user.dm_channel
            if channel is None:
                channel = await user.create_dm()
            return await channel.fetch_message(int(discord_message_id))

        async def try_set_explicit_reaction(
            self,
            target: dict[str, Any],
            reaction: dict[str, Any],
            *,
            previous_reaction: dict[str, Any] | None = None,
            suppress_read_marker: bool = False,
        ) -> bool:
            emoji = self.discord_emoji_from_reaction(reaction)
            if emoji is None:
                return False
            try:
                message = await self.fetch_target_discord_message(target)
                if message is None:
                    return False
                if previous_reaction is not None and not reaction_matches(previous_reaction, reaction):
                    await self.try_remove_display_reaction(target, previous_reaction, message=message)
                if suppress_read_marker and self.target_has_read_receipt(target) and str(emoji) != READ_RECEIPT_EMOJI:
                    await self.try_remove_display_reaction(
                        target,
                        {"emoji": READ_RECEIPT_EMOJI, "emoji_name": READ_RECEIPT_EMOJI},
                        message=message,
                    )
                await message.add_reaction(emoji)
                return True
            except Exception:
                reactions_logger.exception(
                    "failed to set explicit reaction on Discord message %s for user %s",
                    target.get("discord_message_id"),
                    target.get("discord_user_id"),
                )
                return False

        async def try_remove_display_reaction(
            self,
            target: dict[str, Any],
            reaction: dict[str, Any] | None,
            *,
            message: discord.Message | None = None,
        ) -> bool:
            if self.user is None:
                return False
            emoji = self.discord_emoji_from_reaction(reaction)
            if emoji is None:
                return False
            try:
                if message is None:
                    message = await self.fetch_target_discord_message(target)
                if message is None:
                    return False
                await message.remove_reaction(emoji, self.user)
                return True
            except discord.HTTPException:
                return True
            except Exception:
                reactions_logger.exception(
                    "failed to remove reaction from Discord message %s for user %s",
                    target.get("discord_message_id"),
                    target.get("discord_user_id"),
                )
                return False

        async def try_add_receipt_reaction(self, target: dict[str, Any]) -> bool:
            if self.active_reactions_for_target(target):
                return True
            try:
                message = await self.fetch_target_discord_message(target)
                if message is None:
                    return False
                await message.add_reaction(READ_RECEIPT_EMOJI)
                return True
            except Exception:
                receipts_logger.exception(
                    "failed to add read-receipt reaction to Discord message %s for user %s",
                    target.get("discord_message_id"),
                    target.get("discord_user_id"),
                )
                return False

        def packet_type_for_delivery(self, delivery: Delivery) -> str | None:
            return str(delivery.packet.headers.get("arqs_type") or "") or None

        def render_inbound_packet(self, binding: Binding, delivery: Delivery) -> str:
            packet = delivery.packet
            packet_type = self.packet_type_for_delivery(delivery)
            fallback = render_packet_text(body=packet.body, data=packet.data, headers=packet.headers)
            prefix = f"[{binding.label}]"
            if packet_type == TYPE_MESSAGE_V1:
                return f"{prefix} {fallback}"
            if packet_type == TYPE_NOTIFICATION_V1:
                level = str(packet.data.get("level") or "info")
                title = str(packet.data.get("title") or "notification").strip()
                body = str(packet.data.get("body") or "").strip()
                headline = f"{prefix} 🔔 {level}: {title}".strip()
                details = body or fallback
                extras: list[str] = []
                for key in ("source", "host", "script"):
                    value = str(packet.data.get(key) or "").strip()
                    if value:
                        extras.append(f"{key}: {value}")
                tags = packet.data.get("tags")
                if isinstance(tags, list) and tags:
                    extras.append("tags: " + ", ".join(str(item) for item in tags if item not in (None, "")))
                return "\n".join([headline, details, *extras]).strip()
            if packet_type == TYPE_COMMAND_V1:
                return f"{prefix} command.v1\n{packet.body or fallback}"
            if packet_type == TYPE_COMMAND_RESPONSE_V1:
                if isinstance(packet.data, dict):
                    if packet.data.get("ok") is False:
                        detail = str(packet.data.get("error_message") or fallback)
                    else:
                        result = packet.data.get("result")
                        detail = fallback if result is None else json.dumps(result, ensure_ascii=False, indent=2) if not isinstance(result, str) else result
                else:
                    detail = fallback
                return f"{prefix} command.response.v1\n{detail}"
            label = packet_type or "packet"
            return f"{prefix} {label}\n{fallback}"

        async def send_delivered_receipt(self, binding: Binding, delivery: Delivery) -> None:
            if not binding.can_send:
                return
            packet = delivery.packet
            await asyncio.to_thread(
                self.app.send_type,
                arqs_type=TYPE_RECEIPT_RECEIVED_V1,
                data={
                    "receipt_id": str(uuid.uuid4()),
                    "for_packet_id": str(packet.packet_id),
                    "receipt_type": "discord_delivered",
                    "status": "ok",
                    "delivered_to_discord_at": to_iso(utc_now()),
                    "discord_user_id": binding.discord_user_id,
                },
                from_endpoint_id=str(packet.to_endpoint_id),
                to_endpoint_id=str(packet.from_endpoint_id),
                correlation_id=get_correlation_id(packet.headers),
                causation_id=str(packet.packet_id),
                meta={
                    "adapter": "discord_dm",
                    "discord_user_id": binding.discord_user_id,
                },
            )

        async def send_read_receipt(
            self,
            binding: Binding,
            entry: dict[str, Any],
            *,
            discord_user_id: str,
            discord_message_id: str,
        ) -> None:
            await asyncio.to_thread(
                self.app.send_type,
                arqs_type=TYPE_RECEIPT_READ_V1,
                data={
                    "receipt_id": str(uuid.uuid4()),
                    "for_packet_id": str(entry["original_packet_id"]),
                    "receipt_type": "reaction_read",
                    "read_at": to_iso(utc_now()),
                    "discord_user_id": str(discord_user_id),
                    "discord_message_id": str(discord_message_id),
                },
                from_endpoint_id=str(entry["original_to_endpoint_id"]),
                to_endpoint_id=str(entry["original_from_endpoint_id"]),
                correlation_id=None if entry.get("original_correlation_id") in (None, "") else str(entry["original_correlation_id"]),
                causation_id=str(entry["original_packet_id"]),
                meta={
                    "adapter": "discord_dm",
                    "discord_user_id": str(discord_user_id),
                    "discord_message_id": str(discord_message_id),
                },
            )

        async def send_reaction_from_discord_event(
            self,
            binding: Binding,
            entry: dict[str, Any],
            payload: discord.RawReactionActionEvent,
            *,
            action: str,
        ) -> None:
            emoji_payload = serialize_discord_emoji(payload.emoji)
            await asyncio.to_thread(
                self.app.send_reaction,
                for_packet_id=str(entry["original_packet_id"]),
                action=action,
                emoji=emoji_payload.get("emoji"),
                emoji_name=emoji_payload.get("emoji_name"),
                emoji_id=emoji_payload.get("emoji_id"),
                animated=emoji_payload.get("animated"),
                source_platform="discord",
                source_user_id=str(payload.user_id),
                source_message_id=str(payload.message_id),
                from_endpoint_id=str(entry["original_to_endpoint_id"]),
                to_endpoint_id=str(entry["original_from_endpoint_id"]),
                correlation_id=None if entry.get("original_correlation_id") in (None, "") else str(entry["original_correlation_id"]),
                meta={
                    "adapter": "discord_dm",
                    "discord_user_id": str(payload.user_id),
                    "discord_message_id": str(payload.message_id),
                    "discord_channel_id": str(payload.channel_id),
                    "binding_id": binding.binding_id,
                },
            )

        async def reconcile_loop(self) -> None:
            while True:
                try:
                    await self.reconcile_links_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("link reconciliation failed")
                await asyncio.sleep(RECONCILE_INTERVAL_SECONDS)

        async def materialize_pending_binding(
            self,
            local_endpoint_id: str,
            *,
            links: list[Link] | None = None,
        ) -> Binding | None:
            pending = self.state.pending_for_local_endpoint(local_endpoint_id)
            if pending is None:
                return None
            active_links_list = links
            if active_links_list is None:
                client = self.app.require_client()
                active_links_list = [
                    link
                    for link in await asyncio.to_thread(client.list_links)
                    if str(link.status or "active") == "active"
                ]
            matching_link = next(
                (
                    link
                    for link in active_links_list
                    if str(link.endpoint_a_id) == pending.local_endpoint_id or str(link.endpoint_b_id) == pending.local_endpoint_id
                ),
                None,
            )
            if matching_link is None:
                return None
            existing = self.state.get_binding_by_local_endpoint(pending.local_endpoint_id)
            can_send, can_receive = calculate_direction(matching_link, pending.local_endpoint_id)
            label = self.state.ensure_unique_label(
                pending.discord_user_id,
                pending.label,
                exclude_binding_id=None if existing is None else existing.binding_id,
                include_inactive=False,
            )
            now_iso = to_iso(utc_now())
            binding = Binding(
                binding_id=str(uuid.uuid4()) if existing is None else existing.binding_id,
                discord_user_id=pending.discord_user_id,
                local_endpoint_id=pending.local_endpoint_id,
                remote_endpoint_id=resolve_remote_endpoint(matching_link, pending.local_endpoint_id),
                link_id=str(matching_link.link_id),
                label=label if existing is None else existing.label,
                link_mode=str(matching_link.mode),
                can_send=can_send,
                can_receive=can_receive,
                status=str(matching_link.status or "active"),
                created_at=now_iso if existing is None else existing.created_at,
                updated_at=now_iso,
            )
            self.state.upsert_binding(binding)
            self.state.remove_pending_link(pending.pending_id)
            if self.state.get_active_binding(binding.discord_user_id) is None:
                self.state.set_active_binding(binding.discord_user_id, binding.binding_id)
            self.state.save()
            message = (
                f"A new ARQS link is now active as {binding.label}.\n"
                f"Direction: {binding_direction(binding)}\n"
                f"Can send: {'yes' if binding.can_send else 'no'}\n"
                f"Can receive: {'yes' if binding.can_receive else 'no'}"
            )
            try:
                await self.send_user_dm(binding.discord_user_id, message)
                if len(self.state.list_user_bindings(binding.discord_user_id)) == 2:
                    await self.send_user_dm(
                        binding.discord_user_id,
                        "You now have more than one linked contact.\n\n"
                        "How sending works:\n"
                        "- Reply to one of my forwarded messages to answer that contact directly.\n"
                        "- Or use /use_contact to choose the active contact for normal messages.\n\n"
                        "Useful commands:\n"
                        "- /links\n"
                        "- /use_contact\n"
                        "- /current_contact",
                    )
            except Exception:
                links_logger.exception("failed to send pending-link activation DM to Discord user %s", binding.discord_user_id)
            return binding

        async def reconcile_links_once(self) -> None:
            client = self.app.require_client()
            links = await asyncio.to_thread(client.list_links)
            active_link_list = [link for link in links if str(link.status or "active") == "active"]
            active_links = {str(link.link_id): link for link in active_link_list}
            expired_pending = self.state.prune_expired_pending_links()
            changed = bool(expired_pending)
            for pending in expired_pending:
                with contextlib.suppress(Exception):
                    if not self.state.bindings_using_local_endpoint(pending.local_endpoint_id):
                        await asyncio.to_thread(client.delete_endpoint, pending.local_endpoint_id)
            for pending in list(self.state.pending_links):
                binding = await self.materialize_pending_binding(pending.local_endpoint_id, links=active_link_list)
                if binding is not None:
                    changed = True
            for binding in list(self.state.bindings):
                if binding.status != "active":
                    continue
                link = active_links.get(binding.link_id)
                if link is None:
                    binding.status = "severed"
                    binding.updated_at = to_iso(utc_now())
                    self.state.upsert_binding(binding)
                    self.state.remove_binding_indexes(binding.binding_id)
                    changed = True
                    try:
                        await self.send_user_dm(
                            binding.discord_user_id,
                            f"Your ARQS link {binding.label} was severed. Messages will not deliver for that contact until you create a new link.",
                        )
                    except Exception:
                        links_logger.exception("failed to send severed-link DM to Discord user %s", binding.discord_user_id)
                    continue
                can_send, can_receive = calculate_direction(link, binding.local_endpoint_id)
                if (
                    binding.remote_endpoint_id != resolve_remote_endpoint(link, binding.local_endpoint_id)
                    or binding.link_mode != str(link.mode)
                    or binding.can_send != can_send
                    or binding.can_receive != can_receive
                    or binding.status != str(link.status or "active")
                ):
                    binding.remote_endpoint_id = resolve_remote_endpoint(link, binding.local_endpoint_id)
                    binding.link_mode = str(link.mode)
                    binding.can_send = can_send
                    binding.can_receive = can_receive
                    binding.status = str(link.status or "active")
                    binding.updated_at = to_iso(utc_now())
                    self.state.upsert_binding(binding)
                    changed = True
            if changed:
                for user_id in list(self.state.active_contacts.keys()):
                    self.state.ensure_valid_active_binding(user_id)
                self.state.save()
else:
    class ARQSDiscordBot:  # pragma: no cover - runtime dependency fallback
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("discord.py is required to run the Discord bot")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ARQS Discord bot backed by AppKit.")
    parser.add_argument("--state-root", help="Override the AppKit state root. Default: ~/.arqs")
    parser.add_argument("--sync-commands", action="store_true", help="Sync global Discord slash commands on startup.")
    return parser.parse_args()


async def async_main() -> None:
    if _DISCORD_IMPORT_ERROR is not None:
        raise SystemExit("discord.py is not installed. Install it with: pip install -U discord.py")
    token = str(os.getenv("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set.")
    args = parse_args()
    app = ARQSApp.for_app(APP_NAME, state_root=args.state_root)
    ensure_runtime_config(app)
    configure_logging(app.store.paths.log_path, str(app.config.get("discord_log_level") or "INFO"))
    state = DiscordBridgeState(app.store.paths.state_dir / "discord_state.json")
    sync_commands_on_start = bool(args.sync_commands or app.config.get("discord_sync_commands_on_start", False))
    bot = ARQSDiscordBot(app, state, sync_commands_on_start=sync_commands_on_start)
    try:
        await bot.start(token)
    finally:
        if not bot.is_closed():
            with contextlib.suppress(Exception):
                await bot.close()


def main() -> None:
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
