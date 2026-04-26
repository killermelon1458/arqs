from __future__ import annotations

from datetime import datetime, timezone
from threading import Event, Thread
from typing import Any, Callable
import logging
import uuid

from arqs_api import ARQSClient, Endpoint, NodeIdentity
from arqs_conventions import (
    TYPE_MESSAGE_V1,
    TYPE_REACTION_V1,
    build_client_meta,
    build_reaction_key,
    build_v1_headers,
    render_reaction_text,
)

from .commands import CommandDispatcher
from .outbox import SQLiteOutbox
from .receiver import Receiver
from .store import ContactBook, InboxStore, RuntimeStore, replace_identity_default_endpoint
from .transport import TransportResolver
from .types import Contact, SendResult


logger = logging.getLogger("arqs.appkit")


class ARQSApp:
    def __init__(
        self,
        app_name: str,
        *,
        state_root: str | None = None,
        config_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.app_name = str(app_name).strip()
        if not self.app_name:
            raise ValueError("app_name is required")
        self.store = RuntimeStore(self.app_name, state_root=state_root)
        self.config = self.store.load_config()
        if config_overrides:
            self.config.update({key: value for key, value in config_overrides.items() if value is not None})
            self.store.save_config(self.config)
        self.contact_book = ContactBook(self.store)
        self.inbox_store = InboxStore(self.store.paths.inbox_path)
        self.outbox = SQLiteOutbox(self.store.paths.outbox_path)
        self.transport_resolver = TransportResolver()
        self.handlers: dict[str, list[Callable[..., Any]]] = {}
        self.command_dispatcher = CommandDispatcher(self)
        self.receiver = Receiver(self)
        self.client: ARQSClient | None = None
        self.identity: NodeIdentity | None = None
        self._outbox_stop_event = Event()
        self._outbox_thread: Thread | None = None
        self._initialize_runtime_if_configured()

    @classmethod
    def for_app(
        cls,
        app_name: str,
        *,
        state_root: str | None = None,
        **config_overrides: Any,
    ) -> "ARQSApp":
        return cls(app_name, state_root=state_root, config_overrides=config_overrides)

    def require_client(self) -> ARQSClient:
        if self.client is None:
            self._initialize_runtime_if_configured()
        if self.client is None:
            raise ValueError(
                f"AppKit app {self.app_name!r} is missing base_url; run setup or pass base_url when constructing ARQSApp"
            )
        return self.client

    def setup(self, *, save: bool = True, **config_updates: Any) -> "ARQSApp":
        self.config.update({key: value for key, value in config_updates.items() if value is not None})
        if save:
            self.store.save_config(self.config)
        self._initialize_runtime_if_configured(force=True)
        return self

    def on(self, arqs_type: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        key = str(arqs_type or "*")

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.handlers.setdefault(key, []).append(func)
            return func

        return decorator

    def command(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.command_dispatcher.command(name)

    def list_contacts(self) -> list[Contact]:
        return self.contact_book.list_contacts()

    def request_link_code(self, *, source_endpoint_id: str | None = None, requested_mode: str = "bidirectional"):
        client = self.require_client()
        source_id = source_endpoint_id or self.default_endpoint_id
        return client.request_link_code(source_id, requested_mode=requested_mode)

    def redeem_link_code(
        self,
        code: str,
        *,
        label: str,
        destination_endpoint_id: str | None = None,
    ) -> Contact:
        client = self.require_client()
        destination_id = destination_endpoint_id or self.default_endpoint_id
        link = client.redeem_link_code(code, destination_id)
        endpoint_a = str(link.endpoint_a_id)
        endpoint_b = str(link.endpoint_b_id)
        local_id = str(destination_id)
        remote_id = endpoint_b if endpoint_a == local_id else endpoint_a
        return self.contact_book.upsert(
            label=label,
            local_endpoint_id=local_id,
            remote_endpoint_id=remote_id,
            link_id=str(link.link_id),
            status=str(link.status),
        )

    @property
    def default_endpoint_id(self) -> str:
        if self.identity is None:
            self._initialize_runtime_if_configured()
        if self.identity is None:
            raise ValueError("identity is not configured")
        return str(self.identity.default_endpoint_id)

    def send_message(
        self,
        body: str,
        *,
        data: dict[str, Any] | None = None,
        contact: str | None = None,
        delivery_mode: str | None = None,
    ) -> SendResult:
        return self.send_type(
            arqs_type=TYPE_MESSAGE_V1,
            body=body,
            data=data,
            contact=contact,
            delivery_mode=delivery_mode,
        )

    def send_reaction(
        self,
        *,
        for_packet_id: str,
        action: str = "set",
        emoji: str | None = None,
        emoji_name: str | None = None,
        contact: str | None = None,
        from_endpoint_id: str | None = None,
        to_endpoint_id: str | None = None,
        delivery_mode: str | None = None,
        retry_policy: str | None = None,
        max_attempts: int | None = None,
        expires_after_seconds: int | None = None,
        extra_headers: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        correlation_id: str | None = None,
        reaction_id: str | None = None,
        reaction_key: str | None = None,
        source_platform: str = "appkit",
        source_user_id: str | None = None,
        reacted_at: str | None = None,
        emoji_id: str | None = None,
        animated: bool | None = None,
        source_message_id: str | None = None,
    ) -> SendResult:
        normalized_packet_id = str(uuid.UUID(str(for_packet_id)))
        normalized_action = str(action or "").strip().lower()
        if normalized_action not in {"set", "remove"}:
            raise ValueError("reaction action must be 'set' or 'remove'")
        emoji_text = None if emoji in (None, "") else str(emoji).strip()
        emoji_name_text = None if emoji_name in (None, "") else str(emoji_name).strip()
        if not emoji_text and not emoji_name_text:
            raise ValueError("one of emoji or emoji_name is required")
        normalized_source_platform = str(source_platform or "appkit")
        normalized_source_user_id = str(source_user_id or self.app_name)
        normalized_reaction_key = str(reaction_key or "").strip()
        if not normalized_reaction_key:
            normalized_reaction_key = build_reaction_key(
                for_packet_id=normalized_packet_id,
                source_platform=normalized_source_platform,
                source_user_id=normalized_source_user_id,
                emoji=emoji_text,
                emoji_name=emoji_name_text,
                emoji_id=emoji_id,
            )

        data_payload: dict[str, Any] = {
            "reaction_id": str(uuid.UUID(str(reaction_id))) if reaction_id not in (None, "") else str(uuid.uuid4()),
            "reaction_key": normalized_reaction_key,
            "for_packet_id": normalized_packet_id,
            "action": normalized_action,
            "emoji_name": str(emoji_name_text or emoji_text or "").strip(),
            "source_platform": normalized_source_platform,
            "source_user_id": normalized_source_user_id,
            "reacted_at": reacted_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if emoji_text:
            data_payload["emoji"] = emoji_text
        if emoji_id not in (None, ""):
            data_payload["emoji_id"] = str(emoji_id)
        if animated is not None:
            data_payload["animated"] = bool(animated)
        if source_message_id not in (None, ""):
            data_payload["source_message_id"] = str(source_message_id)

        body = render_reaction_text(data_payload) or f"reaction {normalized_action}"
        return self.send_type(
            arqs_type=TYPE_REACTION_V1,
            body=body,
            data=data_payload,
            contact=contact,
            from_endpoint_id=from_endpoint_id,
            to_endpoint_id=to_endpoint_id,
            delivery_mode=delivery_mode,
            retry_policy=retry_policy,
            max_attempts=max_attempts,
            expires_after_seconds=expires_after_seconds,
            extra_headers=extra_headers,
            meta=meta,
            content_type="application/json",
            correlation_id=correlation_id,
            causation_id=normalized_packet_id,
        )

    def send_type(
        self,
        *,
        arqs_type: str,
        body: str | None = None,
        data: dict[str, Any] | None = None,
        contact: str | None = None,
        from_endpoint_id: str | None = None,
        to_endpoint_id: str | None = None,
        delivery_mode: str | None = None,
        retry_policy: str | None = None,
        max_attempts: int | None = None,
        expires_after_seconds: int | None = None,
        extra_headers: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        content_type: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
    ) -> SendResult:
        if not body and not data:
            raise ValueError("at least one of body or non-empty data must be present")
        resolved = self._resolve_route(
            contact=contact,
            from_endpoint_id=from_endpoint_id,
            to_endpoint_id=to_endpoint_id,
        )
        effective_delivery_mode = str(delivery_mode or self.config.get("delivery_mode") or "queued")
        effective_retry_policy = str(retry_policy or self.config.get("retry_policy") or "until_expired")
        effective_max_attempts = max_attempts if max_attempts is not None else self._config_int("max_attempts", 20)
        effective_expires_after_seconds = (
            expires_after_seconds
            if expires_after_seconds is not None
            else self._config_int("expires_after_seconds", 86400)
        )
        effective_content_type = content_type or ("application/json" if data else "text/plain; charset=utf-8")
        headers = build_v1_headers(
            arqs_type=str(arqs_type),
            content_type=effective_content_type,
            correlation_id=correlation_id,
            causation_id=causation_id,
            extra_headers=extra_headers,
        )
        packet_meta = build_client_meta(
            client=f"appkit/{self.app_name}",
            extra_meta=meta,
        )
        packet_data = dict(data or {})
        client = self.require_client()

        if effective_delivery_mode == "direct":
            result = client.send_packet(
                from_endpoint_id=resolved["from_endpoint_id"],
                to_endpoint_id=resolved["to_endpoint_id"],
                body=body,
                data=packet_data,
                headers=headers,
                meta=packet_meta,
            )
            return SendResult(
                packet_id=str(result.packet_id),
                delivery_mode="direct",
                status=result.result,
                delivery_id=None if result.delivery_id is None else str(result.delivery_id),
                expires_at=result.expires_at,
                attempts=1,
            )

        entry = self.outbox.enqueue(
            from_endpoint_id=resolved["from_endpoint_id"],
            to_endpoint_id=resolved["to_endpoint_id"],
            headers=headers,
            body=body,
            data=packet_data,
            meta=packet_meta,
            retry_policy=effective_retry_policy,
            max_attempts=effective_max_attempts,
            expires_after_seconds=effective_expires_after_seconds,
        )

        if effective_delivery_mode == "background":
            self.start_outbox_thread()
            return SendResult(
                packet_id=entry.packet_id,
                delivery_mode="background",
                status="background_queued",
                outbox_id=entry.outbox_id,
            )

        flushed = self.outbox.flush_packet(client, entry.packet_id)
        if flushed.status == "missing":
            return SendResult(
                packet_id=entry.packet_id,
                delivery_mode="queued",
                status="queued",
                outbox_id=entry.outbox_id,
            )
        return flushed

    def flush_outbox(self, *, limit: int = 100) -> list[SendResult]:
        return self.outbox.flush_due(self.require_client(), limit=limit)

    def poll_once(self, *, wait: int | None = None, limit: int | None = None):
        return self.receiver.poll_once(wait=wait, limit=limit)

    def poll_forever(self, *, wait: int | None = None, limit: int | None = None) -> None:
        self.receiver.poll_forever(wait=wait, limit=limit)

    def start_receiver_thread(self, *, wait: int | None = None, limit: int | None = None) -> None:
        self.receiver.start(wait=wait, limit=limit)

    def stop_receiver_thread(self, *, timeout: float = 5.0) -> None:
        self.receiver.stop(timeout=timeout)

    def start_outbox_thread(self, *, interval_seconds: float = 5.0) -> None:
        if self._outbox_thread is not None and self._outbox_thread.is_alive():
            return
        self._outbox_stop_event.clear()

        def worker() -> None:
            while not self._outbox_stop_event.is_set():
                try:
                    self.flush_outbox()
                except Exception:
                    logger.exception("outbox worker flush failed")
                self._outbox_stop_event.wait(interval_seconds)

        self._outbox_thread = Thread(
            target=worker,
            name=f"appkit-outbox-{self.app_name}",
            daemon=True,
        )
        self._outbox_thread.start()

    def stop_outbox_thread(self, *, timeout: float = 5.0) -> None:
        self._outbox_stop_event.set()
        if self._outbox_thread is not None:
            self._outbox_thread.join(timeout=timeout)
        self._outbox_thread = None

    def send_command(self, **kwargs: Any):
        return self.command_dispatcher.send_command(**kwargs)

    def _initialize_runtime_if_configured(self, *, force: bool = False) -> None:
        if self.client is not None and not force:
            return
        base_url = str(self.config.get("base_url") or "").strip()
        if not base_url:
            self.client = None
            self.identity = self.store.load_identity()
            return

        resolution = self.transport_resolver.resolve(
            base_url=base_url,
            transport_policy=str(self.config.get("transport_policy") or "prefer_https"),
            transport_preferences=dict(self.config.get("transport_preferences") or {}),
        )
        preferences = dict(self.config.get("transport_preferences") or {})
        preferences.update(resolution.preference_updates)
        self.config["transport_preferences"] = preferences
        self.store.save_config(self.config)
        self.client = ARQSClient(
            resolution.base_url,
            transport_policy=resolution.transport_policy,
            allow_local_http_auth=resolution.allow_local_http_auth,
        )
        self.identity = self.store.load_identity()
        if self.identity is None:
            self.identity = self.client.register(node_name=str(self.config.get("node_name") or self.app_name))
            self.store.save_identity(self.identity)
        else:
            self.client.adopt_identity(self.identity)
        self._ensure_default_endpoint()

    def _ensure_default_endpoint(self) -> None:
        client = self.require_client()
        endpoints = client.list_endpoints()
        wanted_id = None if self.identity is None else str(self.identity.default_endpoint_id)
        matching = next((endpoint for endpoint in endpoints if str(endpoint.endpoint_id) == wanted_id), None)
        if matching is None:
            endpoint = self._find_or_create_default_endpoint(endpoints)
            if self.identity is None:
                raise ValueError("identity is not configured")
            self.identity = replace_identity_default_endpoint(self.identity, str(endpoint.endpoint_id))
            self.store.save_identity(self.identity)
            client.adopt_identity(self.identity)

    def _find_or_create_default_endpoint(self, endpoints: list[Endpoint]) -> Endpoint:
        wanted_name = str(self.config.get("default_endpoint_name") or "default")
        wanted_kind = str(self.config.get("default_endpoint_kind") or "message")
        for endpoint in endpoints:
            if endpoint.endpoint_name == wanted_name:
                return endpoint
        return self.require_client().create_endpoint(
            endpoint_name=wanted_name,
            kind=wanted_kind,
            meta={"app_name": self.app_name},
        )

    def _resolve_route(
        self,
        *,
        contact: str | None,
        from_endpoint_id: str | None,
        to_endpoint_id: str | None,
    ) -> dict[str, str]:
        if contact and (from_endpoint_id or to_endpoint_id):
            raise ValueError("contact cannot be combined with raw endpoint IDs")
        if contact:
            resolved_contact = self.contact_book.get(contact)
            if resolved_contact is None:
                raise KeyError(f"unknown contact: {contact}")
            return {
                "from_endpoint_id": resolved_contact.local_endpoint_id,
                "to_endpoint_id": resolved_contact.remote_endpoint_id,
            }
        destination = to_endpoint_id
        if destination in (None, ""):
            default_contact = str(self.config.get("default_contact") or "").strip()
            if not default_contact:
                raise ValueError("destination is required: set default_contact or pass contact/to_endpoint_id explicitly")
            resolved_contact = self.contact_book.get(default_contact)
            if resolved_contact is None:
                raise KeyError(f"default contact {default_contact!r} is missing from contacts.json")
            destination = resolved_contact.remote_endpoint_id
            if from_endpoint_id in (None, ""):
                from_endpoint_id = resolved_contact.local_endpoint_id
        return {
            "from_endpoint_id": str(from_endpoint_id or self.default_endpoint_id),
            "to_endpoint_id": str(destination),
        }

    def _config_int(self, key: str, default: int) -> int:
        value = self.config.get(key, default)
        return default if value in (None, "") else int(value)


__all__ = ["ARQSApp"]
