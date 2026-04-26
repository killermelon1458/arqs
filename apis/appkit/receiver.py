from __future__ import annotations

from threading import Event, Thread
from typing import TYPE_CHECKING, Any, Callable
import logging

from arqs_conventions import decode_packet_view, get_packet_type, render_packet_text

from .store import utc_now
from .types import CommandContext, ReceivedPacket

if TYPE_CHECKING:
    from arqs_api import Delivery

    from .app import ARQSApp


logger = logging.getLogger("arqs.appkit.receiver")


class Receiver:
    def __init__(self, app: "ARQSApp") -> None:
        self.app = app
        self._stop_event = Event()
        self._thread: Thread | None = None

    def poll_once(self, *, wait: int | None = None, limit: int | None = None) -> list[ReceivedPacket]:
        client = self.app.require_client()
        deliveries = client.poll_inbox(
            wait=int(self.app.config.get("poll_wait_seconds", 20) if wait is None else wait),
            limit=int(self.app.config.get("poll_limit", 100) if limit is None else limit),
        )
        packets: list[ReceivedPacket] = []
        for delivery in deliveries:
            packet = self._to_received_packet(delivery)
            packets.append(packet)
            self._handle_delivery(delivery, packet)
        return packets

    def poll_forever(self, *, wait: int | None = None, limit: int | None = None) -> None:
        self._stop_event.clear()
        while not self._stop_event.is_set():
            self.poll_once(wait=wait, limit=limit)

    def start(self, *, wait: int | None = None, limit: int | None = None) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = Thread(
            target=self.poll_forever,
            kwargs={"wait": wait, "limit": limit},
            name=f"appkit-receiver-{self.app.app_name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None

    def _handle_delivery(self, delivery: "Delivery", packet: ReceivedPacket) -> None:
        ack_policy = str(self.app.config.get("ack_policy", "after_handler_success"))
        contact = self.app.contact_book.resolve_by_remote_endpoint(packet.from_endpoint_id)
        acked = False

        def ack(status: str = "handled") -> None:
            nonlocal acked
            self.app.require_client().ack_delivery(delivery.delivery_id, status=status)
            acked = True

        def reply(**kwargs: Any):
            return self.app.send_type(
                from_endpoint_id=packet.to_endpoint_id,
                to_endpoint_id=packet.from_endpoint_id,
                delivery_mode="direct",
                **kwargs,
            )

        ctx = CommandContext(
            app=self.app,
            client=self.app.require_client(),
            contact=contact,
            delivery=delivery,
            packet=packet,
            ack=ack,
            reply=reply,
        )

        if ack_policy == "after_store":
            self.app.inbox_store.store_packet(packet)
            ack()

        handled_successfully = False
        try:
            self.app.command_dispatcher.maybe_handle(packet, ctx)
            self._dispatch_handlers(packet, ctx)
            handled_successfully = True
        except Exception:
            logger.exception("receiver handler failure for packet %s", packet.packet_id)
            if ack_policy == "always" and not acked:
                ack("failed")
            raise
        else:
            if ack_policy == "after_handler_success" and not acked:
                ack()
            elif ack_policy == "always" and not acked:
                ack()
            elif ack_policy == "manual":
                pass

        if not handled_successfully:
            logger.warning("packet %s was not marked handled successfully", packet.packet_id)

    def _dispatch_handlers(self, packet: ReceivedPacket, ctx: CommandContext) -> None:
        handlers = list(self.app.handlers.get(packet.arqs_type or "", ()))
        handlers.extend(self.app.handlers.get("*", ()))
        for handler in handlers:
            handler(packet, ctx)

    def _to_received_packet(self, delivery: "Delivery") -> ReceivedPacket:
        decoded = decode_packet_view(body=delivery.packet.body, headers=delivery.packet.headers)
        text = render_packet_text(
            body=delivery.packet.body,
            data=delivery.packet.data,
            headers=delivery.packet.headers,
        )
        return ReceivedPacket(
            delivery_id=str(delivery.delivery_id),
            packet_id=str(delivery.packet.packet_id),
            from_endpoint_id=str(delivery.packet.from_endpoint_id),
            to_endpoint_id=str(delivery.packet.to_endpoint_id),
            arqs_type=get_packet_type(delivery.packet.headers),
            headers=dict(delivery.packet.headers),
            body=delivery.packet.body,
            text=text if text not in ("", "[empty message]") else decoded.body_text,
            data=dict(delivery.packet.data or {}),
            meta=dict(delivery.packet.meta or {}),
            created_at=delivery.packet.created_at,
            received_at=utc_now(),
            decode_errors=tuple(decoded.errors),
        )


__all__ = ["Receiver"]
