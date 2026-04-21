from __future__ import annotations

import pytest


@pytest.mark.cli
def test_stats_and_entity_commands_work_against_seeded_state(admin_cli):
    seeded = admin_cli.seed_transport_state()

    summary = admin_cli.run_json("stats", "summary", "--json")
    assert summary["nodes_total"] == 2
    assert summary["endpoints_total"] == 2
    assert summary["active_links_total"] == 1
    assert summary["queued_packets_total"] == 1
    assert summary["queued_bytes_total"] == 2
    assert summary["active_link_codes_total"] == 1
    assert summary["default_ip_policy"] == "allow"

    queue_by_node = admin_cli.run_json("stats", "queue-by-node", "--json")
    assert len(queue_by_node) == 1
    assert queue_by_node[0]["node_id"] == seeded.node_b_id
    assert queue_by_node[0]["queued_packets"] == 1
    assert queue_by_node[0]["queued_bytes"] == 2

    queue_by_endpoint = admin_cli.run_json("stats", "queue-by-endpoint", "--json")
    assert len(queue_by_endpoint) == 1
    assert queue_by_endpoint[0]["endpoint_id"] == seeded.endpoint_b_id
    assert queue_by_endpoint[0]["queued_packets"] == 1
    assert queue_by_endpoint[0]["queued_bytes"] == 2

    oldest = admin_cli.run_json("stats", "oldest-queued", "--json")
    assert oldest["queued"] is True
    assert oldest["delivery_id"] == seeded.delivery_id
    assert oldest["packet_id"] == seeded.packet_id

    nodes = admin_cli.run_json("nodes", "list", "--json")
    assert {row["node_id"] for row in nodes} == {seeded.node_a_id, seeded.node_b_id}

    node_detail = admin_cli.run_json("nodes", "show", seeded.node_a_id, "--json")
    assert node_detail["node_id"] == seeded.node_a_id
    assert node_detail["endpoint_count"] == 1
    assert node_detail["active_link_count"] == 1
    assert node_detail["active_link_code_count"] == 1

    disabled = admin_cli.run_json("nodes", "disable", seeded.node_a_id, "--json")
    assert disabled["disabled"] is True
    assert disabled["status"] == "disabled"

    enabled = admin_cli.run_json("nodes", "enable", seeded.node_a_id, "--json")
    assert enabled["enabled"] is True
    assert enabled["status"] == "active"

    revoked = admin_cli.run_json("nodes", "revoke", seeded.node_a_id, "--json")
    assert revoked["revoked"] is True
    assert revoked["status"] == "revoked"

    endpoints = admin_cli.run_json("endpoints", "list", "--json")
    assert {row["endpoint_id"] for row in endpoints} == {seeded.endpoint_a_id, seeded.endpoint_b_id}

    endpoint_detail = admin_cli.run_json("endpoints", "show", seeded.endpoint_b_id, "--json")
    assert endpoint_detail["endpoint_id"] == seeded.endpoint_b_id
    assert endpoint_detail["queued_inbound_packets"] == 1
    assert endpoint_detail["queued_inbound_bytes"] == 2
    assert endpoint_detail["active_link_count"] == 1

    links = admin_cli.run_json("links", "list", "--json")
    assert len(links) == 1
    assert links[0]["link_id"] == seeded.link_id
    assert links[0]["status"] == "active"

    link_codes = admin_cli.run_json("link-codes", "list", "--json")
    assert len(link_codes) == 1
    assert link_codes[0]["link_code_id"] == seeded.link_code_id
    assert link_codes[0]["status"] == "active"

    revoke_link = admin_cli.run_json("links", "revoke", seeded.link_id, "--json")
    assert revoke_link["revoked"] is True
    assert revoke_link["routes_revoked"] == 1

    with admin_cli.connect() as conn:
        link_status = conn.execute(
            "SELECT status FROM links WHERE link_id = ?",
            (seeded.link_id,),
        ).fetchone()["status"]
        route_status = conn.execute(
            "SELECT status FROM directed_routes WHERE route_id = ?",
            (seeded.route_id,),
        ).fetchone()["status"]

    assert link_status == "revoked"
    assert route_status == "revoked"


@pytest.mark.cli
@pytest.mark.cleanup
def test_cleanup_run_prunes_expired_records(admin_cli):
    seeded = admin_cli.seed_expired_state()

    cleanup = admin_cli.run_json("cleanup", "run", "--json")
    assert cleanup["expired_link_codes"] == 1
    assert cleanup["expired_packets"] == 1
    assert cleanup["pruned_send_events"] == 1

    with admin_cli.connect() as conn:
        link_code_status = conn.execute(
            "SELECT status FROM link_codes WHERE link_code_id = ?",
            (seeded.link_code_id,),
        ).fetchone()["status"]
        packet_row = conn.execute(
            "SELECT 1 FROM packets WHERE packet_id = ?",
            (seeded.packet_id,),
        ).fetchone()
        delivery_row = conn.execute(
            "SELECT 1 FROM deliveries WHERE delivery_id = ?",
            (seeded.delivery_id,),
        ).fetchone()
        send_events = conn.execute("SELECT COUNT(*) AS total FROM send_events").fetchone()["total"]

    assert link_code_status == "expired"
    assert packet_row is None
    assert delivery_row is None
    assert send_events == 0


@pytest.mark.cli
@pytest.mark.compat
def test_legacy_app_cli_shim_delegates_to_admin_cli(admin_cli):
    data = admin_cli.run_json("health", "--json", module="app.cli")

    assert data["status"] == "ok"
    assert data["runtime_settings"]["default_ip_policy"] == "allow"
