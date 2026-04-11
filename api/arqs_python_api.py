from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib import error, request


@dataclass(slots=True)
class ArqsCredentials:
    actor_id: str
    api_key: str
    client_id: str | None = None
    adapter_type: str | None = None

    def save(self, path: str | Path) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> "ArqsCredentials":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            actor_id=str(data["actor_id"]),
            api_key=str(data["api_key"]),
            client_id=data.get("client_id"),
            adapter_type=data.get("adapter_type"),
        )


@dataclass(slots=True)
class Packet:
    client_id: str
    headers: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    packet_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: int = field(default_factory=lambda: int(time.time()))
    version: int = 1

    def __post_init__(self) -> None:
        if not self.body and not self.data:
            raise ValueError("packet must include body or non-empty data")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "packet_id": self.packet_id,
            "client_id": self.client_id,
            "timestamp": self.timestamp,
            "headers": self.headers,
            "body": self.body,
            "data": self.data,
            "meta": self.meta,
        }


@dataclass(slots=True)
class InboxPacket:
    packet_id: str
    client_id: str
    timestamp: int
    headers: dict[str, Any]
    body: str
    data: dict[str, Any]
    meta: dict[str, Any]
    delivery_kind: str
    delivery: dict[str, Any]


@dataclass(slots=True)
class PacketAckResponse:
    duplicate: bool
    packets_waiting: bool
    waiting_count: int


@dataclass(slots=True)
class LinkRequestResponse:
    link_code: str
    expires_in: int


@dataclass(slots=True)
class RotateKeyResponse:
    actor_id: str
    client_id: str | None
    api_key: str


@dataclass(slots=True)
class RegenerateClientResponse:
    actor_id: str
    old_client_id: str
    client_id: str
    routes_cleared: bool
    relink_required: bool


@dataclass(slots=True)
class AdapterProvisionLinkResponse:
    link_code: str
    adapter_type: str
    expires_in: int


@dataclass(slots=True)
class DeleteIdentityResponse:
    status: str
    actor_id: str
    client_id: str | None


class ArqsApiError(RuntimeError):
    pass


class _BaseHttpApi:
    def __init__(self, server_url: str, request_timeout: float = 30.0) -> None:
        self.server_url = server_url.rstrip("/")
        self.request_timeout = request_timeout

    def _request(
        self,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
        *,
        bearer_token: str | None = None,
    ) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        data: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        req = request.Request(f"{self.server_url}{path}", data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=self.request_timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ArqsApiError(f"HTTP {exc.code} calling {path}: {body}") from exc
        except error.URLError as exc:
            raise ArqsApiError(f"Transport error calling {path}: {exc}") from exc


class ArqsAdminApi(_BaseHttpApi):
    def __init__(self, server_url: str, admin_api_key: str, request_timeout: float = 30.0) -> None:
        super().__init__(server_url=server_url, request_timeout=request_timeout)
        self.admin_api_key = admin_api_key

    def request_adapter_provision_link(self, adapter_type: str, display_name: str | None = None) -> AdapterProvisionLinkResponse:
        payload = {"adapter_type": adapter_type, "display_name": display_name}
        response = self._request("POST", "/admin/adapter-provision/request", payload, bearer_token=self.admin_api_key)
        return AdapterProvisionLinkResponse(
            link_code=str(response["link_code"]),
            adapter_type=str(response["adapter_type"]),
            expires_in=int(response["expires_in"]),
        )

    def revoke_adapter(self, actor_id: str) -> dict[str, Any]:
        return self._request("POST", f"/admin/adapters/{actor_id}/revoke", bearer_token=self.admin_api_key)


class ArqsPythonApi(_BaseHttpApi):
    def __init__(
        self,
        server_url: str,
        credentials: ArqsCredentials,
        state_dir: str | Path,
        request_timeout: float = 30.0,
        retry_interval: float = 30.0,
    ) -> None:
        super().__init__(server_url=server_url, request_timeout=request_timeout)
        self.credentials = credentials
        self.state_dir = Path(state_dir)
        self.retry_interval = retry_interval
        self.queue_dir = self.state_dir / "queue"
        self.credentials_path = self.state_dir / "credentials.json"
        self._retry_thread: threading.Thread | None = None
        self._retry_stop = threading.Event()

        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._persist_credentials()

    @classmethod
    def register_client(
        cls,
        server_url: str,
        state_dir: str | Path,
        request_timeout: float = 30.0,
        retry_interval: float = 30.0,
    ) -> "ArqsPythonApi":
        bootstrap = _BaseHttpApi(server_url=server_url, request_timeout=request_timeout)
        response = bootstrap._request("POST", "/register")
        credentials = ArqsCredentials(
            actor_id=str(response["actor_id"]),
            api_key=str(response["api_key"]),
            client_id=str(response["client_id"]),
        )
        return cls(
            server_url=server_url,
            credentials=credentials,
            state_dir=state_dir,
            request_timeout=request_timeout,
            retry_interval=retry_interval,
        )

    @classmethod
    def register_adapter(
        cls,
        server_url: str,
        state_dir: str | Path,
        link_code: str,
        adapter_type: str,
        display_name: str | None = None,
        request_timeout: float = 30.0,
        retry_interval: float = 30.0,
    ) -> "ArqsPythonApi":
        bootstrap = _BaseHttpApi(server_url=server_url, request_timeout=request_timeout)
        response = bootstrap._request(
            "POST",
            "/adapter-register",
            {
                "link_code": link_code,
                "adapter_type": adapter_type,
                "display_name": display_name,
            },
        )
        credentials = ArqsCredentials(
            actor_id=str(response["actor_id"]),
            api_key=str(response["api_key"]),
            adapter_type=str(response["adapter_type"]),
        )
        return cls(
            server_url=server_url,
            credentials=credentials,
            state_dir=state_dir,
            request_timeout=request_timeout,
            retry_interval=retry_interval,
        )

    @classmethod
    def from_saved(
        cls,
        server_url: str,
        state_dir: str | Path,
        request_timeout: float = 30.0,
        retry_interval: float = 30.0,
    ) -> "ArqsPythonApi":
        state_dir = Path(state_dir)
        credentials = ArqsCredentials.load(state_dir / "credentials.json")
        return cls(
            server_url=server_url,
            credentials=credentials,
            state_dir=state_dir,
            request_timeout=request_timeout,
            retry_interval=retry_interval,
        )

    def _persist_credentials(self) -> None:
        self.credentials.save(self.credentials_path)

    def get_health(self) -> dict[str, Any]:
        return self._request("GET", "/health")

    def get_stats(self) -> dict[str, Any]:
        return self._request("GET", "/stats", bearer_token=self.credentials.api_key)

    def send_packet(self, packet: Packet) -> PacketAckResponse:
        self._enqueue(packet.to_dict())
        return self.flush_queue()

    def flush_queue(self) -> PacketAckResponse:
        duplicate = False
        packets_waiting = False
        waiting_count = 0
        for item in sorted(self.queue_dir.glob("*.json")):
            payload = json.loads(item.read_text(encoding="utf-8"))
            try:
                response = self._request("POST", "/packets", payload, bearer_token=self.credentials.api_key)
            except ArqsApiError:
                continue
            duplicate = bool(response.get("duplicate", False))
            packets_waiting = bool(response.get("packets_waiting", False))
            waiting_count = int(response.get("waiting_count", 0))
            item.unlink(missing_ok=True)
        return PacketAckResponse(duplicate=duplicate, packets_waiting=packets_waiting, waiting_count=waiting_count)

    def poll_inbox(self, wait: int = 0) -> list[InboxPacket]:
        wait = max(0, min(60, int(wait)))
        response = self._request("GET", f"/inbox?wait={wait}", bearer_token=self.credentials.api_key)
        packets: list[InboxPacket] = []
        for raw in response.get("packets", []):
            packets.append(
                InboxPacket(
                    packet_id=str(raw["packet_id"]),
                    client_id=str(raw["client_id"]),
                    timestamp=int(raw["timestamp"]),
                    headers=dict(raw.get("headers", {})),
                    body=str(raw.get("body", "")),
                    data=dict(raw.get("data", {})),
                    meta=dict(raw.get("meta", {})),
                    delivery_kind=str(raw.get("delivery_kind", "")),
                    delivery=dict(raw.get("delivery", {})),
                )
            )
        return packets

    def ack_packet(self, packet_id: str, status: str) -> PacketAckResponse:
        response = self._request(
            "POST",
            "/packet_ack",
            {"packet_id": packet_id, "status": status},
            bearer_token=self.credentials.api_key,
        )
        return PacketAckResponse(
            duplicate=False,
            packets_waiting=bool(response.get("packets_waiting", False)),
            waiting_count=int(response.get("waiting_count", 0)),
        )

    def request_link_code(self) -> LinkRequestResponse:
        response = self._request("POST", "/link_request", bearer_token=self.credentials.api_key)
        return LinkRequestResponse(
            link_code=str(response["link_code"]),
            expires_in=int(response["expires_in"]),
        )

    def complete_link(
        self,
        link_code: str,
        adapter: str,
        external_id: str,
        *,
        filters: Optional[dict[str, Any]] = None,
        config: Optional[dict[str, Any]] = None,
        meta: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload = {
            "link_code": link_code,
            "adapter": adapter,
            "external_id": external_id,
            "filters": filters or {},
            "config": config or {},
            "meta": meta or {},
        }
        return self._request("POST", "/link_complete", payload, bearer_token=self.credentials.api_key)

    def rotate_api_key(self) -> RotateKeyResponse:
        response = self._request("POST", "/identity/rotate-key", bearer_token=self.credentials.api_key)
        self.credentials.api_key = str(response["api_key"])
        self._persist_credentials()
        return RotateKeyResponse(
            actor_id=str(response["actor_id"]),
            client_id=response.get("client_id"),
            api_key=self.credentials.api_key,
        )

    def regenerate_client_id(self, clear_local_queue: bool = True) -> RegenerateClientResponse:
        response = self._request("POST", "/identity/regenerate-client", bearer_token=self.credentials.api_key)
        self.credentials.client_id = str(response["client_id"])
        self._persist_credentials()
        if clear_local_queue:
            self.clear_queue()
        return RegenerateClientResponse(
            actor_id=str(response["actor_id"]),
            old_client_id=str(response["old_client_id"]),
            client_id=str(response["client_id"]),
            routes_cleared=bool(response.get("routes_cleared", True)),
            relink_required=bool(response.get("relink_required", True)),
        )

    def delete_identity(self, purge_local_state: bool = True) -> DeleteIdentityResponse:
        response = self._request("DELETE", "/identity", bearer_token=self.credentials.api_key)
        result = DeleteIdentityResponse(
            status=str(response.get("status", "deleted")),
            actor_id=str(response["actor_id"]),
            client_id=response.get("client_id"),
        )
        if purge_local_state:
            self.stop_retry_worker()
            self.clear_queue()
            self.credentials_path.unlink(missing_ok=True)
        return result

    def start_retry_worker(self) -> None:
        if self._retry_thread and self._retry_thread.is_alive():
            return
        self._retry_stop.clear()
        self._retry_thread = threading.Thread(target=self._retry_loop, name="arqs-python-api-retry", daemon=True)
        self._retry_thread.start()

    def stop_retry_worker(self) -> None:
        self._retry_stop.set()
        if self._retry_thread:
            self._retry_thread.join(timeout=2)
            self._retry_thread = None

    def close(self) -> None:
        self.stop_retry_worker()

    def clear_queue(self) -> None:
        if not self.queue_dir.exists():
            return
        for item in self.queue_dir.glob("*.json"):
            item.unlink(missing_ok=True)

    def _retry_loop(self) -> None:
        while not self._retry_stop.is_set():
            try:
                self.flush_queue()
            except Exception:
                pass
            self._retry_stop.wait(self.retry_interval)

    def _enqueue(self, payload: dict[str, Any]) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        name = f"{int(time.time() * 1000)}-{uuid.uuid4()}.json"
        (self.queue_dir / name).write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


__all__ = [
    "ArqsCredentials",
    "Packet",
    "InboxPacket",
    "PacketAckResponse",
    "LinkRequestResponse",
    "RotateKeyResponse",
    "RegenerateClientResponse",
    "AdapterProvisionLinkResponse",
    "DeleteIdentityResponse",
    "ArqsApiError",
    "ArqsAdminApi",
    "ArqsPythonApi",
]
