from __future__ import annotations

"""
ARQS scripting API client.

This file is intended to track the ARQS server implementation that
currently exists in this repository. It is a low-level transport client,
not a frozen spec snapshot.

Design choices in this client:
- node-centric auth with one API key per node
- endpoint-to-endpoint routing only
- explicit link-code based linking only
- inbox polling at node scope, as exposed by the current server
- transport ACK only; ACK deletes the packet/delivery on the server
- standard-library only; no third-party client dependency required
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import ipaddress
import json
import uuid


LinkMode = Literal["bidirectional", "a_to_b", "b_to_a"]
AckStatus = Literal["received", "handled", "rejected", "failed", "invalid", "unsupported"]
TransportPolicy = Literal["allow_http", "prefer_https", "require_https"]

DEFAULT_API_KEY_HEADER = "X-ARQS-API-Key"
AUTHORIZATION_HEADER = "Authorization"


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


class ARQSInsecureTransportError(ARQSError):
    """Raised when an authenticated request would send credentials over insecure HTTP."""


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
    time: datetime


@dataclass(frozen=True)
class ServerStats:
    nodes_total: int
    endpoints_total: int
    active_links_total: int
    queued_packets_total: int
    queued_bytes_total: int
    link_codes_active_total: int
    time: datetime


@dataclass(frozen=True)
class TransportProbeAttempt:
    requested_url: str
    final_url: str | None
    reachable: bool
    redirected: bool
    status_code: int | None
    error: str | None


@dataclass(frozen=True)
class TransportProbeResult:
    original_base_url: str
    normalized_http_base_url: str | None
    normalized_https_base_url: str | None
    http_attempt: TransportProbeAttempt | None
    https_attempt: TransportProbeAttempt | None
    recommended_base_url: str | None
    classification: str


class _NoRedirectHandler(urllib_request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


ARQSTraceHook = Callable[[dict[str, Any]], None]


class ARQSClient:
    """Thin synchronous client for the ARQS server HTTP API."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        api_key_header: str = DEFAULT_API_KEY_HEADER,
        user_agent: str = "arqs_api.py/1.0",
        transport_policy: TransportPolicy = "prefer_https",
        allow_local_http_auth: bool = True,
        trace_hook: ARQSTraceHook | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.api_key = api_key
        self.timeout = float(timeout)
        self.api_key_header = _normalize_api_key_header(api_key_header)
        self.user_agent = user_agent
        self.transport_policy = _normalize_transport_policy(transport_policy)
        self.allow_local_http_auth = bool(allow_local_http_auth)
        self.trace_hook = trace_hook
        self.identity: NodeIdentity | None = None
        self.last_request_requested_url: str | None = None
        self.last_request_final_url: str | None = None
        self.last_request_redirected = False

    @classmethod
    def from_identity_file(
        cls,
        base_url: str,
        identity_path: str | Path,
        *,
        timeout: float = 30.0,
        api_key_header: str = DEFAULT_API_KEY_HEADER,
        user_agent: str = "arqs_api.py/1.0",
        transport_policy: TransportPolicy = "prefer_https",
        allow_local_http_auth: bool = True,
    ) -> "ARQSClient":
        identity = NodeIdentity.load(identity_path)
        client = cls(
            base_url,
            api_key=identity.api_key,
            timeout=timeout,
            api_key_header=api_key_header,
            user_agent=user_agent,
            transport_policy=transport_policy,
            allow_local_http_auth=allow_local_http_auth,
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

    def set_trace_hook(self, trace_hook: ARQSTraceHook | None) -> None:
        self.trace_hook = trace_hook

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
            time=_parse_datetime(result["time"]),
        )

    def probe_transport(self, base_url: str | None = None, *, timeout: float | None = None) -> TransportProbeResult:
        original_base_url = _normalize_base_url(base_url or self.base_url)
        parts = urllib_parse.urlsplit(original_base_url)
        scheme = parts.scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("ARQS base URL must use http:// or https:// for transport probing")

        normalized_http_base_url = None
        normalized_https_base_url = None
        if scheme == "http":
            normalized_http_base_url = original_base_url
            try:
                normalized_https_base_url = _swap_scheme(original_base_url, "https")
            except ValueError:
                normalized_https_base_url = None
        else:
            normalized_https_base_url = original_base_url
            try:
                normalized_http_base_url = _swap_scheme(original_base_url, "http")
            except ValueError:
                normalized_http_base_url = None

        effective_timeout = self.timeout if timeout is None else float(timeout)
        http_attempt: TransportProbeAttempt | None = None
        https_attempt: TransportProbeAttempt | None = None
        recommended_base_url: str | None = None
        classification = "unreachable"

        if scheme == "https":
            https_attempt = self._probe_health(normalized_https_base_url, timeout=effective_timeout)
            if https_attempt.reachable:
                classification = "https_only"
                recommended_base_url = normalized_https_base_url
            else:
                classification = "https_failed"
        else:
            http_attempt = self._probe_health(normalized_http_base_url, timeout=effective_timeout)
            if normalized_https_base_url is not None:
                https_attempt = self._probe_health(normalized_https_base_url, timeout=effective_timeout)

            if http_attempt.redirected and http_attempt.final_url and _is_https_url(http_attempt.final_url):
                classification = "http_redirects_to_https"
                recommended_base_url = normalized_https_base_url
            elif http_attempt.reachable and https_attempt is not None and https_attempt.reachable:
                classification = "both_http_and_https"
                recommended_base_url = normalized_https_base_url
            elif https_attempt is not None and https_attempt.reachable:
                classification = "https_only"
                recommended_base_url = normalized_https_base_url
            elif http_attempt.reachable:
                classification = "http_only"
                recommended_base_url = normalized_http_base_url
            else:
                classification = "unreachable"

        return TransportProbeResult(
            original_base_url=original_base_url,
            normalized_http_base_url=normalized_http_base_url,
            normalized_https_base_url=normalized_https_base_url,
            http_attempt=http_attempt,
            https_attempt=https_attempt,
            recommended_base_url=recommended_base_url,
            classification=classification,
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
            self._ensure_authenticated_transport_allowed()
            if self.api_key_header == AUTHORIZATION_HEADER:
                token = self.api_key.strip()
                if token.lower().startswith("bearer "):
                    token = token[7:].strip()
                headers[self.api_key_header] = f"Bearer {token}"
            else:
                headers[self.api_key_header] = self.api_key

        request_headers = _redact_trace_headers(headers)
        request_body = body_bytes.decode("utf-8") if body_bytes is not None else None
        self._emit_trace(
            "http_request",
            method=method.upper(),
            path=path,
            url=url,
            params=params or {},
            headers=request_headers,
            body=request_body,
            timeout_seconds=effective_timeout,
            require_auth=require_auth,
        )
        req = urllib_request.Request(url=url, data=body_bytes, headers=headers, method=method.upper())
        try:
            with urllib_request.urlopen(req, timeout=effective_timeout) as response:
                final_url = _normalize_observed_url(response.geturl()) or url
                self._record_last_request(url, final_url)
                raw = response.read().decode("utf-8")
                self._emit_trace(
                    "http_response",
                    method=method.upper(),
                    path=path,
                    url=url,
                    final_url=final_url,
                    redirected=bool(final_url != url),
                    status_code=response.getcode(),
                    headers=dict(response.headers.items()),
                    raw_body=raw,
                )
                if not raw:
                    return None
                return json.loads(raw)
        except urllib_error.HTTPError as exc:
            final_url = _normalize_observed_url(exc.geturl()) or url
            self._record_last_request(url, final_url)
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
            self._emit_trace(
                "http_response_error",
                method=method.upper(),
                path=path,
                url=url,
                final_url=final_url,
                redirected=bool(final_url != url),
                status_code=exc.code,
                headers=dict(exc.headers.items()),
                raw_body=raw,
                detail=detail,
            )
            raise ARQSHTTPError(exc.code, detail, response_json=parsed, response_text=raw) from exc
        except urllib_error.URLError as exc:
            self._record_last_request(url, None)
            self._emit_trace(
                "http_transport_error",
                method=method.upper(),
                path=path,
                url=url,
                error=str(exc),
            )
            raise ARQSConnectionError(f"failed to reach ARQS server at {url}: {exc}") from exc
        except TimeoutError as exc:
            self._record_last_request(url, None)
            self._emit_trace(
                "http_transport_error",
                method=method.upper(),
                path=path,
                url=url,
                error=str(exc),
            )
            raise ARQSConnectionError(f"request to ARQS server timed out after {effective_timeout} seconds") from exc

    def _build_url(self, path: str, *, params: dict[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        if params:
            encoded = urllib_parse.urlencode({k: v for k, v in params.items() if v is not None}, doseq=True)
            if encoded:
                url = f"{url}?{encoded}"
        return url

    def _probe_health(self, base_url: str | None, *, timeout: float) -> TransportProbeAttempt | None:
        if not base_url:
            return None

        requested_url = f"{base_url}/health"
        req = urllib_request.Request(
            url=requested_url,
            headers={"Accept": "application/json", "User-Agent": self.user_agent},
            method="GET",
        )
        opener = urllib_request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(req, timeout=timeout) as response:
                final_url = _normalize_observed_url(response.geturl()) or requested_url
                return TransportProbeAttempt(
                    requested_url=requested_url,
                    final_url=final_url,
                    reachable=True,
                    redirected=final_url != requested_url,
                    status_code=response.getcode(),
                    error=None,
                )
        except urllib_error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                final_url = urllib_parse.urljoin(requested_url, location) if location else None
                normalized_final_url = _normalize_observed_url(final_url)
                return TransportProbeAttempt(
                    requested_url=requested_url,
                    final_url=normalized_final_url,
                    reachable=True,
                    redirected=normalized_final_url is not None and normalized_final_url != requested_url,
                    status_code=exc.code,
                    error=None,
                )
            return TransportProbeAttempt(
                requested_url=requested_url,
                final_url=requested_url,
                reachable=True,
                redirected=False,
                status_code=exc.code,
                error=None,
            )
        except urllib_error.URLError as exc:
            return TransportProbeAttempt(
                requested_url=requested_url,
                final_url=None,
                reachable=False,
                redirected=False,
                status_code=None,
                error=str(exc.reason or exc),
            )
        except TimeoutError as exc:
            return TransportProbeAttempt(
                requested_url=requested_url,
                final_url=None,
                reachable=False,
                redirected=False,
                status_code=None,
                error=str(exc),
            )

    def _ensure_authenticated_transport_allowed(self) -> None:
        if not _is_http_url(self.base_url):
            return
        if self.transport_policy == "allow_http":
            return
        if self.transport_policy == "prefer_https" and self.allow_local_http_auth and _host_is_loopback_or_local(self.base_url):
            return
        raise ARQSInsecureTransportError(
            "Authenticated ARQS request blocked because base URL uses HTTP. Use HTTPS or explicitly allow HTTP transport."
        )

    def _record_last_request(self, requested_url: str, final_url: str | None) -> None:
        self.last_request_requested_url = requested_url
        self.last_request_final_url = final_url
        self.last_request_redirected = bool(final_url and final_url != requested_url)

    def _emit_trace(self, event: str, **payload: Any) -> None:
        if self.trace_hook is None:
            return
        try:
            self.trace_hook(
                {
                    "event": event,
                    "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "base_url": self.base_url,
                    **payload,
                }
            )
        except Exception:
            return


def _stringify_id(value: str | uuid.UUID) -> str:
    if isinstance(value, uuid.UUID):
        return str(value)
    return str(value)


def _normalize_base_url(base_url: str) -> str:
    raw = str(base_url or "").strip()
    if not raw:
        return ""
    parts = urllib_parse.urlsplit(raw)
    if not parts.scheme:
        return raw.rstrip("/")
    normalized_path = parts.path.rstrip("/")
    return urllib_parse.urlunsplit((parts.scheme.lower(), parts.netloc, normalized_path, "", ""))


def _swap_scheme(base_url: str, scheme: str) -> str:
    normalized = _normalize_base_url(base_url)
    parts = urllib_parse.urlsplit(normalized)
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("ARQS base URL must include an http or https scheme and host")
    return urllib_parse.urlunsplit((scheme.lower(), parts.netloc, parts.path, "", ""))


def _is_https_url(base_url: str) -> bool:
    return urllib_parse.urlsplit(_normalize_base_url(base_url)).scheme.lower() == "https"


def _is_http_url(base_url: str) -> bool:
    return urllib_parse.urlsplit(_normalize_base_url(base_url)).scheme.lower() == "http"


def _host_is_loopback_or_local(base_url: str) -> bool:
    host = urllib_parse.urlsplit(_normalize_base_url(base_url)).hostname
    if not host:
        return False
    lowered = host.lower()
    if lowered in {"localhost", "::1"}:
        return True
    try:
        address = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _normalize_observed_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urllib_parse.urlsplit(str(value))
    path = parts.path.rstrip("/")
    return urllib_parse.urlunsplit((parts.scheme.lower(), parts.netloc, path, parts.query, parts.fragment))


def _normalize_transport_policy(value: TransportPolicy | str) -> TransportPolicy:
    policy = str(value or "").strip().lower()
    if policy in {"allow_http", "prefer_https", "require_https"}:
        return policy
    raise ValueError("transport_policy must be 'allow_http', 'prefer_https', or 'require_https'")


def _normalize_api_key_header(value: str) -> str:
    header = str(value or "").strip()
    lowered = header.lower()
    if lowered == DEFAULT_API_KEY_HEADER.lower():
        return DEFAULT_API_KEY_HEADER
    if lowered == AUTHORIZATION_HEADER.lower():
        return AUTHORIZATION_HEADER
    raise ValueError("api_key_header must be 'X-ARQS-API-Key' or 'Authorization'")


def _redact_trace_headers(headers: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in headers.items():
        lowered = str(key).strip().lower()
        if lowered in {DEFAULT_API_KEY_HEADER.lower(), AUTHORIZATION_HEADER.lower()}:
            raw = str(value or "")
            redacted[key] = f"<redacted:{len(raw)} chars>"
        else:
            redacted[key] = value
    return redacted


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
    "ARQSInsecureTransportError",
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
    "TransportProbeAttempt",
    "TransportProbeResult",
    "ARQSClient",
    "LinkMode",
    "AckStatus",
    "TransportPolicy",
]
