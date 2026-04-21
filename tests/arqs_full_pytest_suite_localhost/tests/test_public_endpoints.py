from __future__ import annotations

from .helpers import raw_json_request


def test_health_is_public(server_alive):
    status, body = raw_json_request("GET", "/health")
    assert status == 200
    assert isinstance(body, dict)
    assert body.get("status") == "ok"
    assert "app" in body
    assert "db_path" in body
    assert "time" in body


def test_stats_is_currently_public(server_alive):
    status, body = raw_json_request("GET", "/stats")
    assert status == 200
    assert isinstance(body, dict)
    for key in (
        "nodes_total",
        "endpoints_total",
        "active_links_total",
        "queued_packets_total",
        "queued_bytes_total",
        "link_codes_active_total",
    ):
        assert key in body


def test_register_is_currently_public(server_alive):
    status, body = raw_json_request("POST", "/register", json_body={"node_name": "pytest-public-register-check"})
    assert status == 200
    assert isinstance(body, dict)
    assert "node_id" in body
    assert "api_key" in body
    assert "default_endpoint_id" in body
