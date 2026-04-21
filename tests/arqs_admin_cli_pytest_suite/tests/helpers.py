from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator


def json_blob(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def sqlite_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).replace(tzinfo=None).isoformat(sep=" ")


@dataclass
class SeededTransport:
    node_a_id: str
    node_b_id: str
    endpoint_a_id: str
    endpoint_b_id: str
    link_id: str
    route_id: str
    link_code_id: str
    delivery_id: str
    packet_id: str


@dataclass
class SeededExpiredState:
    node_id: str
    endpoint_id: str
    link_code_id: str
    packet_id: str
    delivery_id: str


@dataclass
class AdminCLIHarness:
    server_dir: Path
    config_path: Path
    db_path: Path

    @property
    def env(self) -> dict[str, str]:
        final_env = os.environ.copy()
        final_env["ARQS_CONFIG"] = str(self.config_path)
        return final_env

    def run_raw(self, *args: str, module: str = "app.admin_cli") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", module, *args],
            cwd=self.server_dir,
            env=self.env,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )

    def run(self, *args: str, module: str = "app.admin_cli", expect: int = 0) -> subprocess.CompletedProcess[str]:
        result = self.run_raw(*args, module=module)
        if result.returncode != expect:
            raise AssertionError(
                f"Expected exit code {expect}, got {result.returncode}\n"
                f"Command: python -m {module} {' '.join(args)}\n"
                f"STDOUT:\n{result.stdout}\n"
                f"STDERR:\n{result.stderr}"
            )
        return result

    def run_json(self, *args: str, module: str = "app.admin_cli", expect: int = 0) -> Any:
        result = self.run(*args, module=module, expect=expect)
        stdout = result.stdout.strip()
        assert stdout, f"Expected JSON output from {' '.join(args)}, got empty stdout"
        return json.loads(stdout)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def bootstrap(self) -> Any:
        return self.run_json("health", "--json")

    def seed_transport_state(self) -> SeededTransport:
        self.bootstrap()

        now = datetime.now(UTC)
        node_a_id = str(uuid.uuid4())
        node_b_id = str(uuid.uuid4())
        endpoint_a_id = str(uuid.uuid4())
        endpoint_b_id = str(uuid.uuid4())
        link_id = str(uuid.uuid4())
        route_id = str(uuid.uuid4())
        link_code_id = str(uuid.uuid4())
        packet_id = str(uuid.uuid4())
        delivery_id = str(uuid.uuid4())

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO nodes (node_id, key_id, api_key_hash, node_name, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (node_a_id, str(uuid.uuid4()), "hash-a", "node-a", sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO nodes (node_id, key_id, api_key_hash, node_name, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (node_b_id, str(uuid.uuid4()), "hash-b", "node-b", sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO endpoints (endpoint_id, node_id, endpoint_name, kind, meta, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (endpoint_a_id, node_a_id, "alpha", "default", json_blob({}), sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO endpoints (endpoint_id, node_id, endpoint_name, kind, meta, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (endpoint_b_id, node_b_id, "beta", "default", json_blob({}), sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO links (link_id, endpoint_a_id, endpoint_b_id, mode, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (link_id, endpoint_a_id, endpoint_b_id, "bidirectional", sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO directed_routes (route_id, from_endpoint_id, to_endpoint_id, created_at, status, created_by_link_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (route_id, endpoint_a_id, endpoint_b_id, sqlite_timestamp(now), "active", link_id),
            )
            conn.execute(
                """
                INSERT INTO link_codes (link_code_id, code, source_endpoint_id, requested_mode, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_code_id,
                    "ABC123",
                    endpoint_a_id,
                    "bidirectional",
                    sqlite_timestamp(now),
                    sqlite_timestamp(now + timedelta(hours=1)),
                    "active",
                ),
            )
            conn.execute(
                """
                INSERT INTO packets (
                    packet_id,
                    version,
                    sender_node_id,
                    from_endpoint_id,
                    to_endpoint_id,
                    headers,
                    body,
                    data,
                    meta,
                    created_at,
                    expires_at,
                    payload_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet_id,
                    1,
                    node_a_id,
                    endpoint_a_id,
                    endpoint_b_id,
                    json_blob({}),
                    "hi",
                    json_blob({}),
                    json_blob({"suite": "admin-cli"}),
                    sqlite_timestamp(now - timedelta(minutes=5)),
                    sqlite_timestamp(now + timedelta(hours=1)),
                    2,
                ),
            )
            conn.execute(
                """
                INSERT INTO deliveries (
                    delivery_id,
                    packet_id,
                    destination_node_id,
                    destination_endpoint_id,
                    queued_at,
                    state,
                    last_attempt_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    packet_id,
                    node_b_id,
                    endpoint_b_id,
                    sqlite_timestamp(now - timedelta(minutes=5)),
                    "queued",
                    None,
                ),
            )

        return SeededTransport(
            node_a_id=node_a_id,
            node_b_id=node_b_id,
            endpoint_a_id=endpoint_a_id,
            endpoint_b_id=endpoint_b_id,
            link_id=link_id,
            route_id=route_id,
            link_code_id=link_code_id,
            delivery_id=delivery_id,
            packet_id=packet_id,
        )

    def seed_expired_state(self) -> SeededExpiredState:
        self.bootstrap()

        now = datetime.now(UTC)
        node_id = str(uuid.uuid4())
        endpoint_id = str(uuid.uuid4())
        link_code_id = str(uuid.uuid4())
        packet_id = str(uuid.uuid4())
        delivery_id = str(uuid.uuid4())

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO nodes (node_id, key_id, api_key_hash, node_name, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (node_id, str(uuid.uuid4()), "hash-cleanup", "cleanup-node", sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO endpoints (endpoint_id, node_id, endpoint_name, kind, meta, created_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (endpoint_id, node_id, "cleanup-endpoint", "default", json_blob({}), sqlite_timestamp(now), "active"),
            )
            conn.execute(
                """
                INSERT INTO link_codes (link_code_id, code, source_endpoint_id, requested_mode, created_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    link_code_id,
                    "ZZZ999",
                    endpoint_id,
                    "a_to_b",
                    sqlite_timestamp(now - timedelta(hours=1)),
                    sqlite_timestamp(now - timedelta(minutes=5)),
                    "active",
                ),
            )
            conn.execute(
                """
                INSERT INTO packets (
                    packet_id,
                    version,
                    sender_node_id,
                    from_endpoint_id,
                    to_endpoint_id,
                    headers,
                    body,
                    data,
                    meta,
                    created_at,
                    expires_at,
                    payload_bytes
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    packet_id,
                    1,
                    node_id,
                    endpoint_id,
                    endpoint_id,
                    json_blob({}),
                    "expired",
                    json_blob({}),
                    json_blob({}),
                    sqlite_timestamp(now - timedelta(hours=2)),
                    sqlite_timestamp(now - timedelta(minutes=1)),
                    7,
                ),
            )
            conn.execute(
                """
                INSERT INTO deliveries (
                    delivery_id,
                    packet_id,
                    destination_node_id,
                    destination_endpoint_id,
                    queued_at,
                    state,
                    last_attempt_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    delivery_id,
                    packet_id,
                    node_id,
                    endpoint_id,
                    sqlite_timestamp(now - timedelta(hours=2)),
                    "queued",
                    None,
                ),
            )
            conn.execute(
                """
                INSERT INTO send_events (event_id, node_id, created_at)
                VALUES (?, ?, ?)
                """,
                (str(uuid.uuid4()), node_id, sqlite_timestamp(now - timedelta(minutes=5))),
            )

        return SeededExpiredState(
            node_id=node_id,
            endpoint_id=endpoint_id,
            link_code_id=link_code_id,
            packet_id=packet_id,
            delivery_id=delivery_id,
        )
