from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from harness import (
    Actor,
    ack_all,
    create_pair,
    docker_start,
    ensure_server_healthy,
    link_bidirectional,
)


def pytest_configure(config):
    config.addinivalue_line("markers", "recovery: crash/restart/downtime recovery tests")
    config.addinivalue_line("markers", "replay: packet-id replay / dedupe behavior tests")
    config.addinivalue_line("markers", "admin: admin-state semantics tests")
    config.addinivalue_line("markers", "docker: tests that stop/start/kill the Docker container")


@pytest.fixture(scope="session", autouse=True)
def server_alive():
    try:
        docker_start()
    except Exception:
        # Container may already be running; just require health.
        ensure_server_healthy()
    return True


@pytest.fixture
def linked_pair(tmp_path: Path):
    sender, receiver = create_pair(tmp_path)
    link_bidirectional(sender, receiver)
    try:
        yield sender, receiver
    finally:
        for actor in (sender, receiver):
            actor.safe_delete_identity()


@pytest.fixture
def packet_id() -> str:
    return str(uuid.uuid4())
