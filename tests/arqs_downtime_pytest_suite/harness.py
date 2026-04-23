from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib import error as urllib_error
from urllib import request as urllib_request

from arqs_api import ARQSClient, ARQSHTTPError

BASE_URL = os.environ.get("ARQS_BASE_URL", "http://localhost:8080").rstrip("/")
DOCKER_CONTAINER = os.environ.get("ARQS_DOCKER_CONTAINER", "arqs-server")
STARTUP_TIMEOUT_SECONDS = int(os.environ.get("ARQS_STARTUP_TIMEOUT_SECONDS", "45"))
WORKER_POLL_WAIT_SECONDS = int(os.environ.get("ARQS_WORKER_POLL_WAIT_SECONDS", "10"))


class HarnessError(RuntimeError):
    pass


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
    def default_endpoint_id(self) -> str:
        assert self.client is not None and self.client.identity is not None
        return str(self.client.identity.default_endpoint_id)

    def reload_client(self) -> ARQSClient:
        assert self.identity_path is not None
        self.client = ARQSClient.from_identity_file(BASE_URL, self.identity_path)
        return self.client

    def safe_delete_identity(self) -> None:
        try:
            if self.client is None and self.identity_path is not None and self.identity_path.exists():
                self.reload_client()
            if self.client is not None:
                self.client.delete_identity(clear_client_identity=True)
        except Exception:
            pass


def unique_name(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def make_actor(tmp_path: Path, prefix: str) -> Actor:
    return Actor(unique_name(prefix), tmp_path / unique_name(prefix)).initialize()


def create_pair(tmp_path: Path) -> tuple[Actor, Actor]:
    return make_actor(tmp_path, "sender"), make_actor(tmp_path, "receiver")


def link_bidirectional(source: Actor, destination: Actor) -> tuple[str, Any]:
    assert source.client is not None and destination.client is not None
    code = source.client.request_link_code(source.default_endpoint_id, requested_mode="bidirectional")
    link = destination.client.redeem_link_code(code.code, destination.default_endpoint_id)
    return code.code, link


def raw_json_request(method: str, path: str, *, timeout: float = 5.0) -> tuple[int, Any]:
    url = f"{BASE_URL}{path}"
    req = urllib_request.Request(url=url, headers={"Accept": "application/json"}, method=method.upper())
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return int(response.status), json.loads(raw) if raw else None
    except urllib_error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else None
        except Exception:
            parsed = raw
        return int(exc.code), parsed
    except Exception as exc:
        raise HarnessError(f"request to {url} failed: {exc}") from exc


def _observability_detail(body: Any) -> str:
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
    if isinstance(body, str):
        return body
    return repr(body)


def _is_public_health_ok(status: int, body: Any) -> bool:
    return (
        status == 200
        and isinstance(body, dict)
        and body.get("status") == "ok"
        and "time" in body
        and "app" not in body
        and "db_path" not in body
    )


def ensure_server_healthy(timeout_seconds: int = STARTUP_TIMEOUT_SECONDS) -> None:
    deadline = time.time() + timeout_seconds
    last_error: str | None = None
    while time.time() < deadline:
        try:
            status, body = raw_json_request("GET", "/health", timeout=3.0)
            if _is_public_health_ok(status, body):
                return
            if status in {401, 403, 404}:
                last_error = (
                    "public /health is unavailable for the downtime harness: "
                    f"HTTP {status} {_observability_detail(body)!r}"
                )
                break
            last_error = f"unexpected /health response: {status} {body!r}"
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.5)
    raise HarnessError(f"server did not become healthy within {timeout_seconds}s: {last_error}")


def docker_cmd(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", *args],
        capture_output=True,
        text=True,
        check=check,
    )


def docker_start() -> None:
    docker_cmd("start", DOCKER_CONTAINER)
    ensure_server_healthy()


def docker_stop_graceful() -> None:
    docker_cmd("stop", DOCKER_CONTAINER)


def docker_kill_hard() -> None:
    docker_cmd("kill", DOCKER_CONTAINER)


def docker_restart_graceful() -> None:
    docker_stop_graceful()
    docker_start()


def docker_restart_hard() -> None:
    docker_kill_hard()
    docker_start()


def worker_script_path() -> Path:
    return Path(__file__).resolve().parent / "workers" / "worker_main.py"


@dataclass
class WorkerHandle:
    process: subprocess.Popen[str]
    output_path: Path
    checkpoint_path: Path | None
    stderr_path: Path

    def wait_for_checkpoint(self, timeout_seconds: float = 20.0) -> None:
        if self.checkpoint_path is None:
            raise HarnessError("worker has no checkpoint path")
        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if self.checkpoint_path.exists():
                return
            if self.process.poll() is not None:
                break
            time.sleep(0.1)
        raise HarnessError(f"worker checkpoint was not reached; stderr={self.stderr_path.read_text(encoding='utf-8', errors='replace')!r}")

    def terminate(self, timeout_seconds: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)

    def kill(self, timeout_seconds: float = 10.0) -> None:
        if self.process.poll() is not None:
            return
        self.process.kill()
        self.process.wait(timeout=timeout_seconds)

    def wait(self, timeout_seconds: float = 30.0) -> int:
        return self.process.wait(timeout=timeout_seconds)

    def read_output_json(self) -> dict[str, Any]:
        if not self.output_path.exists():
            raise HarnessError(f"worker output file does not exist: {self.output_path}")
        return json.loads(self.output_path.read_text(encoding="utf-8"))


def launch_poll_worker(
    *,
    identity_path: Path,
    output_path: Path,
    checkpoint_path: Path | None = None,
    wait_seconds: int = WORKER_POLL_WAIT_SECONDS,
    ack: bool = False,
    hold_after_receive: bool = False,
) -> WorkerHandle:
    stderr_path = output_path.with_suffix(output_path.suffix + ".stderr.txt")
    cmd = [
        sys.executable,
        str(worker_script_path()),
        "poll",
        "--base-url",
        BASE_URL,
        "--identity-path",
        str(identity_path),
        "--wait-seconds",
        str(wait_seconds),
        "--output-path",
        str(output_path),
    ]
    if checkpoint_path is not None:
        cmd.extend(["--checkpoint-path", str(checkpoint_path)])
    if ack:
        cmd.append("--ack")
    if hold_after_receive:
        cmd.append("--hold-after-receive")

    stderr_handle = stderr_path.open("w", encoding="utf-8")
    process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=stderr_handle, text=True)
    return WorkerHandle(process=process, output_path=output_path, checkpoint_path=checkpoint_path, stderr_path=stderr_path)


def fixed_packet_payload(label: str) -> str:
    return f"downtime-test::{label}::{uuid.uuid4().hex}"


def poll_once(actor: Actor, *, wait: int = 0) -> list[Any]:
    assert actor.client is not None
    return actor.client.poll_inbox(wait=wait, limit=100, request_timeout=max(5.0, float(wait) + 5.0))


def ack_all(actor: Actor, deliveries: list[Any]) -> None:
    assert actor.client is not None
    for delivery in deliveries:
        actor.client.ack_delivery(delivery.delivery_id, status="handled")


def assert_single_delivery(deliveries: list[Any], *, packet_id: str | None = None, body: str | None = None) -> Any:
    assert len(deliveries) == 1, f"expected exactly one delivery, got {len(deliveries)}"
    delivery = deliveries[0]
    if packet_id is not None:
        assert str(delivery.packet.packet_id) == packet_id, f"expected packet_id {packet_id}, got {delivery.packet.packet_id}"
    if body is not None:
        assert str(delivery.packet.body) == body, f"expected body {body!r}, got {delivery.packet.body!r}"
    return delivery


def send_fixed_packet(sender: Actor, receiver: Actor, *, packet_id: str, body: str, ttl_seconds: int | None = None):
    assert sender.client is not None
    return sender.client.send_packet(
        from_endpoint_id=sender.default_endpoint_id,
        to_endpoint_id=receiver.default_endpoint_id,
        body=body,
        data=None,
        headers={"content_type": "text/plain"},
        meta={"suite": "downtime"},
        ttl_seconds=ttl_seconds,
        packet_id=packet_id,
    )


def revoke_link_any_side(actor: Actor) -> None:
    assert actor.client is not None
    links = actor.client.list_links()
    assert links, "expected at least one link to revoke"
    actor.client.revoke_link(links[0].link_id)
