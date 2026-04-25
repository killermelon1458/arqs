from __future__ import annotations

import base64
import gzip
import json
from dataclasses import dataclass
from typing import Any, Mapping

ARQS_ENVELOPE_V1 = "v1"
CORE_V1_HEADERS = (
    "arqs_envelope",
    "arqs_type",
    "content_type",
    "content_transfer_encoding",
    "content_encoding",
    "encryption",
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
    extra_headers: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    headers: dict[str, Any] = {
        "arqs_envelope": ARQS_ENVELOPE_V1,
        "arqs_type": str(arqs_type),
        "content_type": str(content_type),
        "content_transfer_encoding": str(content_transfer_encoding),
        "content_encoding": str(content_encoding),
        "encryption": str(encryption),
    }
    if extra_headers:
        headers.update(dict(extra_headers))
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
    value = dict(headers or {}).get("arqs_type")
    if value in (None, ""):
        return None
    return str(value)


def is_convention_v1(headers: Mapping[str, Any] | None) -> bool:
    return str(dict(headers or {}).get("arqs_envelope") or "").strip().lower() == ARQS_ENVELOPE_V1


def decode_packet_view(
    *,
    body: str | None,
    headers: Mapping[str, Any] | None,
) -> DecodedPacketView:
    normalized_headers = dict(headers or {})
    arqs_type = get_packet_type(normalized_headers)
    content_type = _normalize_header_value(normalized_headers.get("content_type"))

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

    encryption = str(normalized_headers["encryption"]).strip().lower()
    if encryption != "none":
        return DecodedPacketView(
            is_convention_v1=True,
            arqs_type=arqs_type,
            content_type=content_type,
            body_text=None,
            body_bytes=None,
            errors=(f"unsupported encryption: {encryption}",),
        )

    transfer_encoding = str(normalized_headers["content_transfer_encoding"]).strip().lower()
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

    content_encoding = str(normalized_headers["content_encoding"]).strip().lower()
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

    if data:
        return json.dumps(dict(data), ensure_ascii=False, indent=2)

    if decoded.errors:
        label = decoded.arqs_type or "packet"
        return f"[{label}: {decoded.errors[0]}]"

    return "[empty message]"


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


__all__ = [
    "ARQS_ENVELOPE_V1",
    "CORE_V1_HEADERS",
    "DecodedPacketView",
    "build_client_meta",
    "build_v1_headers",
    "decode_packet_view",
    "get_packet_type",
    "is_convention_v1",
    "render_packet_text",
]
