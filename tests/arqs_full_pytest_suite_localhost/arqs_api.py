from __future__ import annotations

"""
ARQS scripting API client.

This file is intentionally aligned to the API actually implemented in
`arqs-server-v0.1.0.zip` and the transport model described in
`ARQS_Server_Plan.md`.

Design choices in this client:
- node-centric auth with one API key per node
- endpoint-to-endpoint routing only
- explicit link-code based linking only
- inbox polling at node scope, as exposed by the current server
- transport ACK only; ACK deletes the packet/delivery on the server
- standard-library only; no third-party client dependency required
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import json
import uuid


LinkMode = Literal["bidirectional", "a_to_b", "b_to_a"]
AckStatus = Literal["received", "handled", "rejected", "failed", "invalid", "unsupported"]


class ARQSError(Exception):
    """Base ARQS client error."""


class ARQSHTTPError(ARQSError):
    """Raised when the ARQS server returns a non-2xx response."""

    def __init__(
        self,
        status_code: int,
        detail: Any,
        *,
        response_json: Any | None = None,
        response_text: str | None = None,
    ) -> None:
        self.status_code = status_code
        self.detail = detail
        self.response_json = response_json
        self.response_text = response_text
        super().__init__(f"ARQS HTTP {status_code}: {self._detail_string(detail)}")

    @staticmethod
    def _detail_string(detail: Any) -> str:
        if isinstance(detail, str):
            return detail
        try:
            return json.dumps(detail, ensure_ascii=False)
        except Exception:
            return repr(detail)


class ARQSConnectionError(ARQSError):
    """Raised for local transport issues such as DNS, TCP, or timeout failures."""


@dataclass(frozen=True)
class NodeIdentity:
    node_id: uuid.UUID
    api_key: str
    default_endpoint_id: uuid.UUID

    def to_dict(self) -> dict[str, str]:
        return {
            "node_id": str(self.node_id),
            "api_key": self.api_key,
            "default_endpoint_id": str(self.default_endpoint_id),
        }

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target

    @classmethod
    def load(cls, path: str | Path) -> "NodeIdentity":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            node_id=_parse_uuid(raw["node_id"]),
            api_key=str(raw["api_key"]),
            default_endpoint_id=_parse_uuid(raw["default_endpoint_id"]),
        )


@dataclass(frozen=True)
class RotatedKey:
    node_id: uuid.UUID
    api_key: str

@dataclass(frozen=True)
class IdentityDeleteResult:
    deleted: bool
    node_id: uuid.UUID
    endpoints_deleted: int
    links_deleted: int
    routes_deleted: int
    link_codes_deleted: int
    packets_deleted: int
    deliveries_deleted: int
    send_events_deleted: int


@dataclass(frozen=True)
class Endpoint:
    endpoint_id: uuid.UUID
    node_id: uuid.UUID
    endpoint_name: str | None
    kind: str | None
    meta: dict[str, Any] | None
    created_at: datetime
    status: str


@dataclass(frozen=True)
class LinkCode:
    link_code_id: uuid.UUID
    code: str
    source_endpoint_id: uuid.UUID
    requested_mode: LinkMode
    created_at: datetime
    expires_at: datetime
    status: str


@dataclass(frozen=True)
class Link:
    link_id: uuid.UUID
    endpoint_a_id: uuid.UUID
    endpoint_b_id: uuid.UUID
    mode: str
    created_at: datetime
    status: str


@dataclass(frozen=True)
class PacketSendResult:
    result: Literal["accepted", "duplicate"]
    packet_id: uuid.UUID
    delivery_id: uuid.UUID | None
    expires_at: datetime | None


@dataclass(frozen=True)
class DeliveryPacket:
    packet_id: uuid.UUID
    version: int
    from_endpoint_id: uuid.UUID
    to_endpoint_id: uuid.UUID
    headers: dict[str, Any]
    body: str | None
    data: dict[str, Any]
    meta: dict[str, Any]
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class Delivery:
    delivery_id: uuid.UUID
    destination_endpoint_id: uuid.UUID
    queued_at: datetime
    state: str
    last_attempt_at: datetime | None
    packet: DeliveryPacket


@dataclass(frozen=True)
class HealthStatus:
    status: str
    app: str
    db_path: str
    time: datetime


@dataclass(frozen=True)
class ServerStats:
    nodes_total: int
    endpoints_total: int
    active_links_total: int
    queued_packets_total: int
    queued_bytes_total: int
    link_codes_active_total: int


class ARQSClient:
    """Thin synchronous client for the ARQS server HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        api_key_header: str = "X-ARQS-API-Key",
        user_agent: str = "arqs_api.py/1.0",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = float(timeout)
        self.api_key_header = api_key_header
        self.user_agent = user_agent
        self.identity: NodeIdentity | None = None

    @classmethod
    def from_identity_file(
        cls,
        base_url: str,
        identity_path: str | Path,
        *,
        timeout: float = 30.0,
        api_key_header: str = "X-ARQS-API-Key",
        user_agent: str = "arqs_api.py/1.0",
    ) -> "ARQSClient":
        identity = NodeIdentity.load(identity_path)
        client = cls(
            base_url,
            api_key=identity.api_key,
            timeout=timeout,
            api_key_header=api_key_header,
            user_agent=user_agent,
        )
        client.identity = identity
        return client

    def set_api_key(self, api_key: str | None) -> None:
        self.api_key = api_key
        if self.identity is not None and api_key != self.identity.api_key:
            self.identity = None

    def adopt_identity(self, identity: NodeIdentity) -> None:
        self.identity = identity
        self.api_key = identity.api_key

    def save_identity(self, path: str | Path) -> Path:
        if self.identity is None:
            raise ARQSError("no identity is currently loaded on this client")
        return self.identity.save(path)

    def register(self, node_name: str | None = None, *, adopt_identity: bool = True) -> NodeIdentity:
        payload: dict[str, Any] = {}
        if node_name is not None:
            payload["node_name"] = node_name
        data = self._request_json("POST", "/register", json_body=payload)
        identity = NodeIdentity(
            node_id=_parse_uuid(data["node_id"]),
            api_key=str(data["api_key"]),
            default_endpoint_id=_parse_uuid(data["default_endpoint_id"]),
        )
        if adopt_identity:
            self.adopt_identity(identity)
        return identity

    def rotate_key(self, *, update_client_key: bool = True) -> RotatedKey:
        data = self._request_json("POST", "/identity/rotate-key", require_auth=True)
        rotated = RotatedKey(node_id=_parse_uuid(data["node_id"]), api_key=str(data["api_key"]))
        if update_client_key:
            self.api_key = rotated.api_key
            if self.identity is not None and self.identity.node_id == rotated.node_id:
                self.identity = NodeIdentity(
                    node_id=self.identity.node_id,
                    api_key=rotated.api_key,
                    default_endpoint_id=self.identity.default_endpoint_id,
                )
        return rotated

    def delete_identity(self, *, clear_client_identity: bool = True) -> IdentityDeleteResult:
        data = self._request_json("DELETE", "/identity", require_auth=True)
        result = IdentityDeleteResult(
            deleted=bool(data["deleted"]),
            node_id=_parse_uuid(data["node_id"]),
            endpoints_deleted=int(data["endpoints_deleted"]),
            links_deleted=int(data["links_deleted"]),
            routes_deleted=int(data["routes_deleted"]),
            link_codes_deleted=int(data["link_codes_deleted"]),
            packets_deleted=int(data["packets_deleted"]),
            deliveries_deleted=int(data["deliveries_deleted"]),
            send_events_deleted=int(data["send_events_deleted"]),
        )
        if clear_client_identity:
            self.api_key = None
            self.identity = None
        return result

    def list_endpoints(self) -> list[Endpoint]:
        data = self._request_json("GET", "/endpoints", require_auth=True)
        return [_parse_endpoint(item) for item in data]

    def create_endpoint(
        self,
        *,
        endpoint_name: str | None = None,
        kind: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Endpoint:
        payload = {
            "endpoint_name": endpoint_name,
            "kind": kind,
            "meta": meta,
        }
        data = self._request_json("POST", "/endpoints", json_body=payload, require_auth=True)
        return _parse_endpoint(data)

    def delete_endpoint(self, endpoint_id: str | uuid.UUID) -> dict[str, Any]:
        return self._request_json("DELETE", f"/endpoints/{_stringify_id(endpoint_id)}", require_auth=True)

    def request_link_code(
        self,
        source_endpoint_id: str | uuid.UUID,
        *,
        requested_mode: LinkMode = "bidirectional",
    ) -> LinkCode:
        payload = {
            "source_endpoint_id": _stringify_id(source_endpoint_id),
            "requested_mode": requested_mode,
        }
        data = self._request_json("POST", "/links/request", json_body=payload, require_auth=True)
        return _parse_link_code(data)

    def redeem_link_code(self, code: str, destination_endpoint_id: str | uuid.UUID) -> Link:
        payload = {
            "code": code.strip().upper(),
            "destination_endpoint_id": _stringify_id(destination_endpoint_id),
        }
        data = self._request_json("POST", "/links/redeem", json_body=payload, require_auth=True)
        return _parse_link(data)

    def list_links(self) -> list[Link]:
        data = self._request_json("GET", "/links", require_auth=True)
        return [_parse_link(item) for item in data]

    def revoke_link(self, link_id: str | uuid.UUID) -> dict[str, Any]:
        return self._request_json("DELETE", f"/links/{_stringify_id(link_id)}", require_auth=True)

    def send_packet(
        self,
        *,
        from_endpoint_id: str | uuid.UUID,
        to_endpoint_id: str | uuid.UUID,
        body: str | None = None,
        data: dict[str, Any] | None = None,
        headers: dict[str, Any] | None = None,
        meta: dict[str, Any] | None = None,
        ttl_seconds: int | None = None,
        packet_id: str | uuid.UUID | None = None,
        version: int = 1,
    ) -> PacketSendResult:
        data_payload = data or {}
        if not body and not data_payload:
            raise ValueError("at least one of body or non-empty data must be present")
        payload: dict[str, Any] = {
            "version": version,
            "packet_id": _stringify_id(packet_id or uuid.uuid4()),
            "from_endpoint_id": _stringify_id(from_endpoint_id),
            "to_endpoint_id": _stringify_id(to_endpoint_id),
            "headers": headers or {},
            "body": body,
            "data": data_payload,
            "meta": meta or {},
        }
        if ttl_seconds is not None:
            payload["ttl_seconds"] = int(ttl_seconds)
        result = self._request_json("POST", "/packets", json_body=payload, require_auth=True)
        return PacketSendResult(
            result=result["result"],
            packet_id=_parse_uuid(result["packet_id"]),
            delivery_id=_parse_uuid_or_none(result.get("delivery_id")),
            expires_at=_parse_datetime_or_none(result.get("expires_at")),
        )

    def poll_inbox(
        self,
        *,
        wait: int = 0,
        limit: int = 100,
        request_timeout: float | None = None,
    ) -> list[Delivery]:
        timeout = request_timeout if request_timeout is not None else max(self.timeout, float(wait) + 5.0)
        params = {"wait": int(wait), "limit": int(limit)}
        result = self._request_json("GET", "/inbox", params=params, require_auth=True, timeout=timeout)
        return [_parse_delivery(item) for item in result.get("deliveries", [])]

    def ack_delivery(self, delivery_id: str | uuid.UUID, *, status: AckStatus = "handled") -> dict[str, Any]:
        payload = {
            "delivery_id": _stringify_id(delivery_id),
            "status": status,
        }
        return self._request_json("POST", "/packet_ack", json_body=payload, require_auth=True)

    def ack_packet(self, packet_id: str | uuid.UUID, *, status: AckStatus = "handled") -> dict[str, Any]:
        payload = {
            "packet_id": _stringify_id(packet_id),
            "status": status,
        }
        return self._request_json("POST", "/packet_ack", json_body=payload, require_auth=True)

    def health(self) -> HealthStatus:
        result = self._request_json("GET", "/health")
        return HealthStatus(
            status=str(result["status"]),
            app=str(result["app"]),
            db_path=str(result["db_path"]),
            time=_parse_datetime(result["time"]),
        )

    def stats(self) -> ServerStats:
        result = self._request_json("GET", "/stats")
        return ServerStats(
            nodes_total=int(result["nodes_total"]),
            endpoints_total=int(result["endpoints_total"]),
            active_links_total=int(result["active_links_total"]),
            queued_packets_total=int(result["queued_packets_total"]),
            queued_bytes_total=int(result["queued_bytes_total"]),
            link_codes_active_total=int(result["link_codes_active_total"]),
        )

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        require_auth: bool = False,
        timeout: float | None = None,
    ) -> Any:
        effective_timeout = self.timeout if timeout is None else float(timeout)
        url = self._build_url(path, params=params)
        headers = {
            "Accept": "application/json",
            "User-Agent": self.user_agent,
        }
        body_bytes: bytes | None = None
        if json_body is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(json_body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if require_auth:
            if not self.api_key:
                raise ARQSError("this request requires an API key, but no API key is set")
            headers[self.api_key_header] = self.api_key

        req = urllib_request.Request(url=url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(req, timeout=effective_timeout) as response:
                raw = response.read().decode("utf-8")
                if not raw:
                    return None
                return json.loads(raw)
        except urllib_error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            parsed: Any | None = None
            detail: Any = raw
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "detail" in parsed:
                        detail = parsed["detail"]
                    else:
                        detail = parsed
                except Exception:
                    detail = raw
            raise ARQSHTTPError(exc.code, detail, response_json=parsed, response_text=raw) from exc
        except urllib_error.URLError as exc:
            raise ARQSConnectionError(f"failed to reach ARQS server at {url}: {exc}") from exc
        except TimeoutError as exc:
            raise ARQSConnectionError(f"request to ARQS server timed out after {effective_timeout} seconds") from exc

    def _build_url(self, path: str, *, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        if params:
            encoded = urllib_parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            if encoded:
                url = f"{url}?{encoded}"
        return url


def _stringify_id(value: str | uuid.UUID) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _parse_uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _parse_uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return _parse_uuid(value)


def _parse_datetime(value: Any) -> datetime:
    dt = datetime.fromisoformat(str(value))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_datetime_or_none(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    return _parse_datetime(value)


def _parse_endpoint(data: dict[str, Any]) -> Endpoint:
    return Endpoint(
        endpoint_id=_parse_uuid(data["endpoint_id"]),
        node_id=_parse_uuid(data["node_id"]),
        endpoint_name=data.get("endpoint_name"),
        kind=data.get("kind"),
        meta=data.get("meta"),
        created_at=_parse_datetime(data["created_at"]),
        status=str(data["status"]),
    )


def _parse_link_code(data: dict[str, Any]) -> LinkCode:
    return LinkCode(
        link_code_id=_parse_uuid(data["link_code_id"]),
        code=str(data["code"]),
        source_endpoint_id=_parse_uuid(data["source_endpoint_id"]),
        requested_mode=data["requested_mode"],
        created_at=_parse_datetime(data["created_at"]),
        expires_at=_parse_datetime(data["expires_at"]),
        status=str(data["status"]),
    )


def _parse_link(data: dict[str, Any]) -> Link:
    return Link(
        link_id=_parse_uuid(data["link_id"]),
        endpoint_a_id=_parse_uuid(data["endpoint_a_id"]),
        endpoint_b_id=_parse_uuid(data["endpoint_b_id"]),
        mode=str(data["mode"]),
        created_at=_parse_datetime(data["created_at"]),
        status=str(data["status"]),
    )


def _parse_delivery(data: dict[str, Any]) -> Delivery:
    packet_data = data["packet"]
    packet = DeliveryPacket(
        packet_id=_parse_uuid(packet_data["packet_id"]),
        version=int(packet_data["version"]),
        from_endpoint_id=_parse_uuid(packet_data["from_endpoint_id"]),
        to_endpoint_id=_parse_uuid(packet_data["to_endpoint_id"]),
        headers=dict(packet_data.get("headers") or {}),
        body=packet_data.get("body"),
        data=dict(packet_data.get("data") or {}),
        meta=dict(packet_data.get("meta") or {}),
        created_at=_parse_datetime(packet_data["created_at"]),
        expires_at=_parse_datetime_or_none(packet_data.get("expires_at")),
    )
    return Delivery(
        delivery_id=_parse_uuid(data["delivery_id"]),
        destination_endpoint_id=_parse_uuid(data["destination_endpoint_id"]),
        queued_at=_parse_datetime(data["queued_at"]),
        state=str(data["state"]),
        last_attempt_at=_parse_datetime_or_none(data.get("last_attempt_at")),
        packet=packet,
    )


__all__ = [
    "ARQSError",
    "ARQSHTTPError",
    "ARQSConnectionError",
    "NodeIdentity",
    "RotatedKey",
    "IdentityDeleteResult",
    "Endpoint",
    "LinkCode",
    "Link",
    "PacketSendResult",
    "DeliveryPacket",
    "Delivery",
    "HealthStatus",
    "ServerStats",
    "ARQSClient",
    "LinkMode",
    "AckStatus",
]
