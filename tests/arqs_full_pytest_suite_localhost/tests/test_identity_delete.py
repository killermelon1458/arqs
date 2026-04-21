# test_identity_delete.py
from __future__ import annotations

import pytest

from .helpers import Actor, assert_http_error, create_endpoint, link_bidirectional, raw_json_request, unique_name


def test_delete_identity_invalidates_api_key_and_clears_client(tmp_path, server_alive):
    actor = Actor(unique_name("delete-identity"), tmp_path / "delete-identity").initialize()

    old_key = actor.api_key
    old_node_id = actor.node_id

    result = actor.client.delete_identity()

    assert result.deleted is True
    assert str(result.node_id) == old_node_id

    # Client-side clear behavior from arqs_api.delete_identity(clear_client_identity=True)
    assert actor.client.api_key is None
    assert actor.client.identity is None

    # Old key must no longer authenticate
    status, body = raw_json_request(
        "GET",
        "/endpoints",
        headers={"X-ARQS-API-Key": old_key},
    )
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("detail") == "invalid api key"


def test_delete_identity_cascades_links_routes_codes_packets_and_deliveries(actor_trio):
    a, b, _ = actor_trio

    old_a_key = a.api_key
    old_a_node_id = a.node_id
    old_a_default_endpoint_id = a.default_endpoint_id

    # Give A a second endpoint so endpoint deletion count is meaningful.
    extra_endpoint_id = create_endpoint(a, endpoint_name="a-extra")

    # Create one active bidirectional link sourced from A.
    _code, _link = link_bidirectional(a, b)

    # Send one packet from B to A and leave it queued/unacked so A owns the delivery.
    send_result = b.client.send_packet(
        from_endpoint_id=b.default_endpoint_id,
        to_endpoint_id=a.default_endpoint_id,
        body="hello-a",
    )
    assert send_result.result == "accepted"

    result = a.client.delete_identity()

    assert result.deleted is True
    assert str(result.node_id) == old_a_node_id

    # Exact counts for this controlled setup:
    # - A has default endpoint + 1 extra endpoint
    # - 1 active link involving A
    # - 2 directed routes from the bidirectional link
    # - 1 link code sourced from A's endpoint
    # - 1 packet addressed to A
    # - 1 delivery owned by A
    # - A did not send any packets in this setup, so send_events should be 0
    assert result.endpoints_deleted == 2
    assert result.links_deleted == 1
    assert result.routes_deleted == 2
    assert result.link_codes_deleted == 1
    assert result.packets_deleted == 1
    assert result.deliveries_deleted == 1
    assert result.send_events_deleted == 0

    # Old key must be dead immediately after deletion.
    status, body = raw_json_request(
        "GET",
        "/endpoints",
        headers={"X-ARQS-API-Key": old_a_key},
    )
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("detail") == "invalid api key"

    # B should no longer see any active links involving deleted A.
    links_b = b.client.list_links()
    assert links_b == []

    # Sending to A's deleted endpoint must now fail.
    with pytest.raises(Exception) as exc_info:
        b.client.send_packet(
            from_endpoint_id=b.default_endpoint_id,
            to_endpoint_id=old_a_default_endpoint_id,
            body="should fail after identity delete",
        )
    assert_http_error(exc_info, 404, "destination endpoint not found")