from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Callable
import logging
import uuid

from arqs_conventions import (
    TYPE_COMMAND_RESPONSE_V1,
    TYPE_COMMAND_V1,
    get_correlation_id,
)

from .store import to_iso, utc_now
from .types import CommandContext, CommandResponse, ReceivedPacket, SendResult

if TYPE_CHECKING:
    from .app import ARQSApp


logger = logging.getLogger("arqs.appkit.commands")


@dataclass
class _PendingCommand:
    event: Event = field(default_factory=Event)
    response: CommandResponse | None = None


class CommandDispatcher:
    def __init__(self, app: "ARQSApp") -> None:
        self.app = app
        self._handlers: dict[str, Callable[[dict[str, Any], CommandContext], Any]] = {}
        self._pending: dict[str, _PendingCommand] = {}
        self._lock = Lock()

    def command(self, name: str) -> Callable[[Callable[[dict[str, Any], CommandContext], Any]], Callable[[dict[str, Any], CommandContext], Any]]:
        normalized = str(name).strip()
        if not normalized:
            raise ValueError("command name is required")

        def decorator(func: Callable[[dict[str, Any], CommandContext], Any]) -> Callable[[dict[str, Any], CommandContext], Any]:
            self._handlers[normalized] = func
            return func

        return decorator

    def send_command(
        self,
        *,
        contact: str | None = None,
        command: str,
        args: dict[str, Any] | None = None,
        timeout_seconds: float = 30.0,
        wait_for_response: bool = True,
        delivery_mode: str | None = None,
        from_endpoint_id: str | None = None,
        to_endpoint_id: str | None = None,
    ) -> CommandResponse | SendResult:
        correlation_id = str(uuid.uuid4())
        command_id = str(uuid.uuid4())
        waiter = _PendingCommand()
        if wait_for_response:
            with self._lock:
                self._pending[correlation_id] = waiter

        send_result: SendResult | None = None
        try:
            send_result = self.app.send_type(
                arqs_type=TYPE_COMMAND_V1,
                body=f"command {command}",
                data={
                    "args": dict(args or {}),
                    "command": str(command),
                    "command_id": command_id,
                    "created_at": to_iso(utc_now()),
                },
                contact=contact,
                from_endpoint_id=from_endpoint_id,
                to_endpoint_id=to_endpoint_id,
                delivery_mode=delivery_mode,
                correlation_id=correlation_id,
            )
            if not wait_for_response:
                return send_result
            if not waiter.event.wait(timeout_seconds):
                raise TimeoutError(f"timed out waiting for command response for correlation_id {correlation_id}")
            if waiter.response is None:
                raise TimeoutError(f"missing command response for correlation_id {correlation_id}")
            return waiter.response
        finally:
            if wait_for_response:
                with self._lock:
                    self._pending.pop(correlation_id, None)

    def maybe_handle(self, packet: ReceivedPacket, ctx: CommandContext) -> bool:
        if packet.arqs_type == TYPE_COMMAND_RESPONSE_V1:
            return self._handle_response(packet)
        if packet.arqs_type != TYPE_COMMAND_V1:
            return False
        return self._handle_command(packet, ctx)

    def _handle_response(self, packet: ReceivedPacket) -> bool:
        correlation_id = get_correlation_id(packet.headers)
        if correlation_id is None:
            return False
        response = CommandResponse(
            ok=bool(packet.data.get("ok")),
            command_id=str(packet.data.get("command_id") or ""),
            correlation_id=correlation_id,
            result=packet.data.get("result"),
            error_type=None if packet.data.get("error_type") in (None, "") else str(packet.data.get("error_type")),
            error_message=None if packet.data.get("error_message") in (None, "") else str(packet.data.get("error_message")),
            received_at=packet.received_at,
            packet=packet,
        )
        with self._lock:
            waiter = self._pending.get(correlation_id)
            if waiter is None:
                return False
            waiter.response = response
            waiter.event.set()
        return True

    def _handle_command(self, packet: ReceivedPacket, ctx: CommandContext) -> bool:
        command_name = str(packet.data.get("command") or "").strip()
        command_id = str(packet.data.get("command_id") or "")
        args = packet.data.get("args")
        if not isinstance(args, dict):
            args = {}
        handler = self._handlers.get(command_name)
        if handler is None:
            self._send_error_response(
                ctx,
                packet=packet,
                command_id=command_id,
                error_type="LookupError",
                error_message=f"unknown command: {command_name or '<missing>'}",
            )
            return True
        try:
            result = handler(dict(args), ctx)
        except Exception as exc:
            logger.exception("command handler failed for %s", command_name)
            self._send_error_response(
                ctx,
                packet=packet,
                command_id=command_id,
                error_type=exc.__class__.__name__,
                error_message=str(exc),
            )
            return True

        ctx.reply(
            arqs_type=TYPE_COMMAND_RESPONSE_V1,
            body=f"command {command_name} ok",
            data={
                "command_id": command_id,
                "ok": True,
                "responded_at": to_iso(utc_now()),
                "result": result,
            },
            correlation_id=get_correlation_id(packet.headers),
            causation_id=packet.packet_id,
        )
        return True

    def _send_error_response(
        self,
        ctx: CommandContext,
        *,
        packet: ReceivedPacket,
        command_id: str,
        error_type: str,
        error_message: str,
    ) -> None:
        ctx.reply(
            arqs_type=TYPE_COMMAND_RESPONSE_V1,
            body=f"command failed: {error_message}",
            data={
                "command_id": command_id,
                "ok": False,
                "error_type": str(error_type),
                "error_message": str(error_message),
                "responded_at": to_iso(utc_now()),
            },
            correlation_id=get_correlation_id(packet.headers),
            causation_id=packet.packet_id,
        )


__all__ = ["CommandDispatcher"]
