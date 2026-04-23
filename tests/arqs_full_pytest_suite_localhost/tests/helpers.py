from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from arqs_api import ARQSClient, ARQSHTTPError

BASE_URL = os.environ.get("ARQS_BASE_URL", "http://localhost:8080").rstrip("/")


def raw_json_request(
    method: str,
    path: str,
    *,
    json_body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> tuple[int, Any]:
    url = f"{BASE_URL}{path}"
    final_headers = {"Accept": "application/json", "User-Agent": "arqs-pytest-suite/1.0"}
    if headers:
        final_headers.update(headers)
    data = None
    if json_body is not None:
        final_headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    req = urllib.request.Request(url=url, data=data, headers=final_headers, method=method.upper())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            parsed = json.loads(raw) if raw else None
            return int(response.status), parsed
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw
        return int(exc.code), parsed


def observability_detail(body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
    if isinstance(body, str):
        return body
    return repr(body)


def assert_health_response_schema(body: Any) -> None:
    assert isinstance(body, dict), f"Expected /health JSON object, got {body!r}"
    assert body.get("status") == "ok", f"Expected /health status='ok', got {body!r}"
    assert "time" in body, f"Expected /health to include time, got {body!r}"
    assert "app" not in body, f"/health should no longer expose app, got {body!r}"
    assert "db_path" not in body, f"/health should no longer expose db_path, got {body!r}"


def assert_http_error(exc_info, status_code: int, detail_contains: str | None = None) -> None:
    exc = exc_info.value
    assert isinstance(exc, ARQSHTTPError), f"Expected ARQSHTTPError, got {type(exc).__name__}: {exc}"
    assert exc.status_code == status_code, f"Expected HTTP {status_code}, got HTTP {exc.status_code}: {exc.detail}"
    if detail_contains is not None:
        assert detail_contains in str(exc.detail), f"Expected detail to contain {detail_contains!r}, got {exc.detail!r}"


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Actor:
    name: str
    base_dir: Path
    client: ARQSClient | None = None
    identity_path: Path | None = None

    def initialize(self) -> "Actor":
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.identity_path = self.base_dir / "identity.json"
        self.client = ARQSClient(BASE_URL)
        identity = self.client.register(node_name=self.name)
        identity.save(self.identity_path)
        self.client.adopt_identity(identity)
        return self

    @property
    def node_id(self) -> str:
        assert self.client is not None and self.client.identity is not None
        return str(self.client.identity.node_id)

    @property
    def api_key(self) -> str:
        assert self.client is not None and self.client.identity is not None
        return str(self.client.identity.api_key)

    @property
    def default_endpoint_id(self) -> str:
        assert self.client is not None and self.client.identity is not None
        return str(self.client.identity.default_endpoint_id)

    def new_client_from_saved_identity(self) -> ARQSClient:
        assert self.identity_path is not None
        return ARQSClient.from_identity_file(BASE_URL, self.identity_path)

    def load_from_identity_path(self, path: Path) -> ARQSClient:
        return ARQSClient.from_identity_file(BASE_URL, path)


def make_actor(tmp_path: Path, prefix: str) -> Actor:
    return Actor(unique_name(prefix), tmp_path / unique_name(prefix)).initialize()


def create_trio(tmp_path: Path) -> tuple[Actor, Actor, Actor]:
    return (
        make_actor(tmp_path, "client-a"),
        make_actor(tmp_path, "client-b"),
        make_actor(tmp_path, "client-c"),
    )


def create_endpoint(actor: Actor, *, endpoint_name: str | None = None, kind: str = "message") -> str:
    assert actor.client is not None
    ep = actor.client.create_endpoint(endpoint_name=endpoint_name or unique_name("ep"), kind=kind, meta=None)
    return str(ep.endpoint_id)


def link_bidirectional(source: Actor, destination: Actor, *, source_endpoint_id: str | None = None, destination_endpoint_id: str | None = None) -> tuple[str, Any]:
    assert source.client is not None and destination.client is not None
    source_ep = source_endpoint_id or source.default_endpoint_id
    dest_ep = destination_endpoint_id or destination.default_endpoint_id
    code = source.client.request_link_code(source_ep, requested_mode="bidirectional")
    link = destination.client.redeem_link_code(code.code, dest_ep)
    return code.code, link


def link_mode(source: Actor, destination: Actor, *, mode: str, source_endpoint_id: str | None = None, destination_endpoint_id: str | None = None) -> tuple[str, Any]:
    assert source.client is not None and destination.client is not None
    source_ep = source_endpoint_id or source.default_endpoint_id
    dest_ep = destination_endpoint_id or destination.default_endpoint_id
    code = source.client.request_link_code(source_ep, requested_mode=mode)
    link = destination.client.redeem_link_code(code.code, dest_ep)
    return code.code, link


def concurrent_call(fn_a, fn_b) -> tuple[list[Any], list[BaseException]]:
    results: list[Any] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def runner(fn):
        try:
            result = fn()
            with lock:
                results.append(result)
        except BaseException as exc:
            with lock:
                errors.append(exc)

    t1 = threading.Thread(target=runner, args=(fn_a,), daemon=True)
    t2 = threading.Thread(target=runner, args=(fn_b,), daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    return results, errors
