from __future__ import annotations

import base64
import gzip
import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

HEADER_ARQS_ENVELOPE = "arqs_envelope"
HEADER_ARQS_TYPE = "arqs_type"
HEADER_CONTENT_TYPE = "content_type"
HEADER_CONTENT_TRANSFER_ENCODING = "content_transfer_encoding"
HEADER_CONTENT_ENCODING = "content_encoding"
HEADER_ENCRYPTION = "encryption"
HEADER_RECEIPT_REQUEST = "receipt_request"
HEADER_CORRELATION_ID = "correlation_id"
HEADER_CAUSATION_ID = "causation_id"

ARQS_ENVELOPE_V1 = "v1"
TYPE_MESSAGE_V1 = "message.v1"
TYPE_NOTIFICATION_V1 = "notification.v1"
TYPE_SCRIPT_FAILURE_V1 = "script.failure.v1"
TYPE_SCRIPT_FAILURE_TRACEBACK_V1 = "script.failure.traceback.v1"
TYPE_RECEIPT_RECEIVED_V1 = "receipt.received.v1"
TYPE_RECEIPT_PROCESSED_V1 = "receipt.processed.v1"
TYPE_RECEIPT_READ_V1 = "receipt.read.v1"
TYPE_REACTION_V1 = "reaction.v1"
TYPE_COMMAND_V1 = "command.v1"
TYPE_COMMAND_RESPONSE_V1 = "command.response.v1"
TYPE_PRESENCE_PING_V1 = "presence.ping.v1"
TYPE_PRESENCE_PONG_V1 = "presence.pong.v1"

CORE_V1_HEADERS = (
    HEADER_ARQS_ENVELOPE,
    HEADER_ARQS_TYPE,
    HEADER_CONTENT_TYPE,
    HEADER_CONTENT_TRANSFER_ENCODING,
    HEADER_CONTENT_ENCODING,
    HEADER_ENCRYPTION,
)


@dataclass(frozen=True)
class DecodedPacketView:
    is_convention_v1: bool
    arqs_type: str | None
    content_type: str | None
    body_text: str | None
    body_bytes: bytes | None
    errors: tuple[str, ...]


def build_v1_headers(
    arqs_type: str,
    *,
    content_type: str,
    content_transfer_encoding: str = "utf-8",
    content_encoding: str = "identity",
    encryption: str = "none",
    receipt_request: list[str] | tuple[str, ...] | None = None,
    correlation_id: str | uuid.UUID | None = None,
    causation_id: str | uuid.UUID | None = None,
    extra_headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    headers: dict[str, Any] = dict(extra_headers or {})
    headers.update(
        {
            HEADER_ARQS_ENVELOPE: ARQS_ENVELOPE_V1,
            HEADER_ARQS_TYPE: str(arqs_type),
            HEADER_CONTENT_TYPE: str(content_type),
            HEADER_CONTENT_TRANSFER_ENCODING: str(content_transfer_encoding),
            HEADER_CONTENT_ENCODING: str(content_encoding),
            HEADER_ENCRYPTION: str(encryption),
        }
    )
    normalized_receipt_request = _normalize_receipt_request(receipt_request)
    if normalized_receipt_request:
        headers[HEADER_RECEIPT_REQUEST] = list(normalized_receipt_request)
    normalized_correlation_id = _normalize_uuid_header_value(correlation_id)
    if normalized_correlation_id is not None:
        headers[HEADER_CORRELATION_ID] = normalized_correlation_id
    normalized_causation_id = _normalize_uuid_header_value(causation_id)
    if normalized_causation_id is not None:
        headers[HEADER_CAUSATION_ID] = normalized_causation_id
    return headers


def build_client_meta(
    *,
    client: str,
    adapter: str | None = None,
    extra_meta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {"client": str(client)}
    if adapter:
        meta["adapter"] = str(adapter)
    if extra_meta:
        meta.update(dict(extra_meta))
    return meta


def get_packet_type(headers: Mapping[str, Any] | None) -> str | None:
    value = dict(headers or {}).get(HEADER_ARQS_TYPE)
    if value in (None, ""):
        return None
    return str(value)


def get_correlation_id(headers: Mapping[str, Any] | None) -> str | None:
    return _get_uuid_header_value(headers, HEADER_CORRELATION_ID)


def get_causation_id(headers: Mapping[str, Any] | None) -> str | None:
    return _get_uuid_header_value(headers, HEADER_CAUSATION_ID)


def get_receipt_request(headers: Mapping[str, Any] | None) -> tuple[str, ...]:
    return _normalize_receipt_request(dict(headers or {}).get(HEADER_RECEIPT_REQUEST))


def is_receipt_type(arqs_type: str | None) -> bool:
    return str(arqs_type or "").startswith("receipt.")


def is_presence_ping_type(arqs_type: str | None) -> bool:
    return arqs_type == TYPE_PRESENCE_PING_V1


def is_presence_pong_type(arqs_type: str | None) -> bool:
    return arqs_type == TYPE_PRESENCE_PONG_V1


def should_ignore_receipt_request(headers: Mapping[str, Any] | None) -> bool:
    return is_receipt_type(get_packet_type(headers))


def build_receipt_headers(
    receipt_arqs_type: str,
    *,
    original_headers: Mapping[str, Any] | None,
    original_packet_id: str | uuid.UUID,
) -> dict[str, Any]:
    return build_v1_headers(
        receipt_arqs_type,
        content_type="application/json",
        correlation_id=get_correlation_id(original_headers),
        causation_id=original_packet_id,
    )


def build_reaction_key(
    *,
    for_packet_id: str | uuid.UUID,
    source_platform: str,
    source_user_id: str,
    emoji: str | None = None,
    emoji_name: str | None = None,
    emoji_id: str | None = None,
) -> str:
    emoji_identity = get_reaction_emoji_identity(
        {
            "emoji": emoji,
            "emoji_name": emoji_name,
            "emoji_id": emoji_id,
        }
    )
    if emoji_identity is None:
        raise ValueError("one of emoji_id, emoji, or emoji_name is required")
    platform = str(source_platform or "").strip().lower()
    if not platform:
        raise ValueError("source_platform is required")
    user_id = str(source_user_id or "").strip()
    if not user_id:
        raise ValueError("source_user_id is required")
    payload = {
        "for_packet_id": str(uuid.UUID(str(for_packet_id))),
        "source_platform": platform,
        "source_user_id": user_id,
        "emoji_kind": emoji_identity[0],
        "emoji_value": emoji_identity[1],
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "reaction.v1:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def get_reaction_key(data: Mapping[str, Any] | None) -> str | None:
    payload = dict(data or {})
    reaction_key = str(payload.get("reaction_key") or "").strip()
    if reaction_key:
        return reaction_key
    try:
        return build_reaction_key(
            for_packet_id=payload.get("for_packet_id"),
            source_platform=str(payload.get("source_platform") or ""),
            source_user_id=str(payload.get("source_user_id") or ""),
            emoji=None if payload.get("emoji") in (None, "") else str(payload.get("emoji")),
            emoji_name=None if payload.get("emoji_name") in (None, "") else str(payload.get("emoji_name")),
            emoji_id=None if payload.get("emoji_id") in (None, "") else str(payload.get("emoji_id")),
        )
    except (TypeError, ValueError, AttributeError):
        return None


def get_reaction_emoji_identity(data: Mapping[str, Any] | None) -> tuple[str, str] | None:
    payload = dict(data or {})
    emoji_id = str(payload.get("emoji_id") or "").strip()
    if emoji_id:
        return ("emoji_id", emoji_id)
    emoji = str(payload.get("emoji") or "").strip()
    if emoji:
        return ("emoji", emoji)
    emoji_name = str(payload.get("emoji_name") or "").strip().strip(":")
    if emoji_name:
        return ("emoji_name", emoji_name)
    return None


def is_convention_v1(headers: Mapping[str, Any] | None) -> bool:
    return str(dict(headers or {}).get(HEADER_ARQS_ENVELOPE) or "").strip().lower() == ARQS_ENVELOPE_V1


def decode_packet_view(
    *,
    body: str | None,
    headers: Mapping[str, Any] | None,
) -> DecodedPacketView:
    normalized_headers = dict(headers or {})
    arqs_type = get_packet_type(normalized_headers)
    content_type = _normalize_header_value(normalized_headers.get(HEADER_CONTENT_TYPE))

    if not is_convention_v1(normalized_headers):
        legacy_text = None if body is None else str(body)
        legacy_bytes = None if body is None else legacy_text.encode("utf-8")
        return DecodedPacketView(
            is_convention_v1=False,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=legacy_text,
            body_bytes=legacy_bytes,
            errors=(),
        )

    missing = [name for name in CORE_V1_HEADERS if _normalize_header_value(normalized_headers.get(name)) is None]
    if missing:
        return DecodedPacketView(
            is_convention_v1=True,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=None,
            body_bytes=None,
            errors=(f"missing required convention header(s): {', '.join(missing)}",),
        )

    encryption = str(normalized_headers[HEADER_ENCRYPTION]).strip().lower()
    if encryption != "none":
        return DecodedPacketView(
            is_convention_v1=True,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=None,
            body_bytes=None,
            errors=(f"unsupported encryption: {encryption}",),
        )

    transfer_encoding = str(normalized_headers[HEADER_CONTENT_TRANSFER_ENCODING]).strip().lower()
    try:
        raw_bytes = _decode_transfer_encoding(body, transfer_encoding)
    except ValueError as exc:
        return DecodedPacketView(
            is_convention_v1=True,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=None,
            body_bytes=None,
            errors=(str(exc),),
        )

    content_encoding = str(normalized_headers[HEADER_CONTENT_ENCODING]).strip().lower()
    try:
        decoded_bytes = _decode_content_encoding(raw_bytes, content_encoding)
    except ValueError as exc:
        return DecodedPacketView(
            is_convention_v1=True,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=None,
            body_bytes=None,
            errors=(str(exc),),
        )

    body_text = None
    if _is_textual_content_type(content_type):
        charset = _content_charset(content_type) or "utf-8"
        try:
            body_text = decoded_bytes.decode(charset)
        except UnicodeDecodeError as exc:
            return DecodedPacketView(
                is_convention_v1=True,
                arqs_type=arqs_type,
                content_type=content_type,
                body_text=None,
                body_bytes=decoded_bytes,
                errors=(f"failed to decode text payload with charset {charset}: {exc}",),
            )

    return DecodedPacketView(
        is_convention_v1=True,
        arqs_type=arqs_type,
        content_type=content_type,
        body_text=body_text,
        body_bytes=decoded_bytes,
        errors=(),
    )


def render_packet_text(
    *,
    body: str | None,
    data: Mapping[str, Any] | None,
    headers: Mapping[str, Any] | None,
) -> str:
    decoded = decode_packet_view(body=body, headers=headers)
    if decoded.body_text not in (None, ""):
        return str(decoded.body_text)

    if decoded.arqs_type == TYPE_REACTION_V1 and data:
        reaction_text = render_reaction_text(data)
        if reaction_text is not None:
            return reaction_text

    if data:
        return json.dumps(dict(data), ensure_ascii=False, indent=2)

    if decoded.errors:
        label = decoded.arqs_type or "packet"
        return f"[{label}: {decoded.errors[0]}]"

    return "[empty message]"


def render_reaction_text(data: Mapping[str, Any] | None) -> str | None:
    payload = dict(data or {})
    action = str(payload.get("action") or "").strip().lower()
    display = _reaction_display_value(payload)
    if not display:
        return None
    if action == "set":
        return f"reacted with {display}"
    if action == "remove":
        return f"removed reaction {display}"
    return None


def _decode_transfer_encoding(body: str | None, transfer_encoding: str) -> bytes:
    if body is None:
        return b""
    if transfer_encoding == "utf-8":
        return str(body).encode("utf-8")
    if transfer_encoding == "base64":
        try:
            return base64.b64decode(str(body), validate=True)
        except Exception as exc:
            raise ValueError(f"invalid base64 body: {exc}") from exc
    raise ValueError(f"unsupported content_transfer_encoding: {transfer_encoding}")


def _decode_content_encoding(raw_bytes: bytes, content_encoding: str) -> bytes:
    if content_encoding == "identity":
        return raw_bytes
    if content_encoding == "gzip":
        try:
            return gzip.decompress(raw_bytes)
        except Exception as exc:
            raise ValueError(f"invalid gzip payload: {exc}") from exc
    raise ValueError(f"unsupported content_encoding: {content_encoding}")


def _normalize_header_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _normalize_uuid_header_value(value: str | uuid.UUID | None) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"invalid UUID header value: {value}") from exc


def _get_uuid_header_value(headers: Mapping[str, Any] | None, key: str) -> str | None:
    return _normalize_optional_uuid_header_value(dict(headers or {}).get(key))


def _normalize_optional_uuid_header_value(value: Any) -> str | None:
    if value in (None, ""):
        return None
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _normalize_receipt_request(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        return ()
    normalized: list[str] = []
    for item in value:
        if item in (None, ""):
            continue
        text = str(item).strip()
        if text:
            normalized.append(text)
    return tuple(normalized)


def _is_textual_content_type(content_type: str | None) -> bool:
    lowered = str(content_type or "").strip().lower()
    if not lowered:
        return False
    if lowered.startswith("text/"):
        return True
    if lowered.startswith("application/json"):
        return True
    if "+json" in lowered:
        return True
    return False


def _content_charset(content_type: str | None) -> str | None:
    lowered = str(content_type or "")
    for part in lowered.split(";")[1:]:
        key, _, value = part.strip().partition("=")
        if key.strip().lower() == "charset" and value.strip():
            return value.strip().strip('"').strip("'")
    return None


def _reaction_display_value(data: Mapping[str, Any]) -> str:
    emoji = data.get("emoji")
    if emoji not in (None, ""):
        emoji_text = str(emoji).strip()
        if emoji_text:
            return emoji_text
    emoji_name = str(data.get("emoji_name") or "").strip()
    if not emoji_name:
        return ""
    if emoji_name.startswith(":") and emoji_name.endswith(":"):
        return emoji_name
    if " " in emoji_name:
        return emoji_name
    return f":{emoji_name}:"


__all__ = [
    "ARQS_ENVELOPE_V1",
    "CORE_V1_HEADERS",
    "DecodedPacketView",
    "HEADER_ARQS_ENVELOPE",
    "HEADER_ARQS_TYPE",
    "HEADER_CAUSATION_ID",
    "HEADER_CONTENT_ENCODING",
    "HEADER_CONTENT_TRANSFER_ENCODING",
    "HEADER_CONTENT_TYPE",
    "HEADER_CORRELATION_ID",
    "HEADER_ENCRYPTION",
    "HEADER_RECEIPT_REQUEST",
    "TYPE_COMMAND_RESPONSE_V1",
    "TYPE_COMMAND_V1",
    "TYPE_MESSAGE_V1",
    "TYPE_NOTIFICATION_V1",
    "TYPE_PRESENCE_PING_V1",
    "TYPE_PRESENCE_PONG_V1",
    "TYPE_RECEIPT_PROCESSED_V1",
    "TYPE_RECEIPT_READ_V1",
    "TYPE_RECEIPT_RECEIVED_V1",
    "TYPE_REACTION_V1",
    "TYPE_SCRIPT_FAILURE_TRACEBACK_V1",
    "TYPE_SCRIPT_FAILURE_V1",
    "build_client_meta",
    "build_reaction_key",
    "build_receipt_headers",
    "build_v1_headers",
    "decode_packet_view",
    "get_causation_id",
    "get_correlation_id",
    "get_packet_type",
    "get_reaction_emoji_identity",
    "get_reaction_key",
    "get_receipt_request",
    "is_convention_v1",
    "is_presence_ping_type",
    "is_presence_pong_type",
    "is_receipt_type",
    "render_packet_text",
    "render_reaction_text",
    "should_ignore_receipt_request",
]
