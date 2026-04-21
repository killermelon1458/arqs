from __future__ import annotations

import time
import pytest
import uuid

from .helpers import assert_http_error, link_bidirectional, link_mode, raw_json_request


def test_send_requires_active_route(actor_trio):
    a, b, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body="no route yet",
        )
    assert_http_error(exc_info, 403, "no active directed route")


def test_bidirectional_route_allows_send_both_ways(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    res1 = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="a->b")
    res2 = b.client.send_packet(from_endpoint_id=b.default_endpoint_id, to_endpoint_id=a.default_endpoint_id, body="b->a")
    assert res1.result == "accepted"
    assert res2.result == "accepted"


def test_one_way_a_to_b_blocks_reverse(actor_trio):
    a, b, _ = actor_trio
    link_mode(a, b, mode="a_to_b")
    res = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="allowed")
    assert res.result == "accepted"

    with pytest.raises(Exception) as exc_info:
        b.client.send_packet(from_endpoint_id=b.default_endpoint_id, to_endpoint_id=a.default_endpoint_id, body="blocked")
    assert_http_error(exc_info, 403, "no active directed route")


def test_send_from_foreign_endpoint_rejected(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(from_endpoint_id=b.default_endpoint_id, to_endpoint_id=a.default_endpoint_id, body="forged sender")
    assert_http_error(exc_info, 403, "endpoint not owned by node")


def test_send_to_nonexistent_endpoint_rejected(actor_trio):
    a, _, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id="00000000-0000-0000-0000-000000000000",
            body="missing destination",
        )
    assert_http_error(exc_info, 404, "destination endpoint not found")


def test_duplicate_packet_id_same_payload_returns_duplicate(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    packet_id = str(uuid.uuid4())

    first = a.client.send_packet(
        packet_id=packet_id,
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="same body",
        headers={"content_type": "text/plain"},
        meta={"case": "duplicate-same"},
    )
    second = a.client.send_packet(
        packet_id=packet_id,
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="same body",
        headers={"content_type": "text/plain"},
        meta={"case": "duplicate-same"},
    )

    assert first.result == "accepted"
    assert second.result == "duplicate"
    assert str(first.packet_id) == str(second.packet_id)


def test_duplicate_packet_id_different_payload_conflicts(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    packet_id = str(uuid.uuid4())

    a.client.send_packet(
        packet_id=packet_id,
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="one",
    )
    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            packet_id=packet_id,
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body="two",
        )
    assert_http_error(exc_info, 409, "packet_id already used for different packet")


def test_send_requires_body_or_data(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    with pytest.raises(ValueError):
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body=None,
            data=None,
        )


def test_expired_packet_id_can_be_reused(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    packet_id = str(uuid.uuid4())

    first = a.client.send_packet(
        packet_id=packet_id,
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="expires-soon",
        ttl_seconds=1,
    )
    assert first.result == "accepted"

    time.sleep(1.2)

    second = a.client.send_packet(
        packet_id=packet_id,
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="fresh-payload",
    )
    assert second.result == "accepted"


@pytest.mark.slow
def test_oversize_packet_rejected(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    body = "X" * 300_000
    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body=body,
        )
    assert_http_error(exc_info, 400, "packet exceeds max packet bytes")
