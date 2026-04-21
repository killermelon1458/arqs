from __future__ import annotations

import time

import pytest

from .helpers import assert_http_error, link_bidirectional


def test_inbox_only_returns_own_deliveries(actor_trio):
    a, b, c = actor_trio
    link_bidirectional(a, b)
    a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="hello-b")

    inbox_b = b.client.poll_inbox(wait=0, limit=100)
    inbox_c = c.client.poll_inbox(wait=0, limit=100)

    assert len(inbox_b) >= 1
    assert len(inbox_c) == 0
    assert all(str(item.packet.to_endpoint_id) == b.default_endpoint_id for item in inbox_b)


def test_poll_without_ack_returns_same_delivery_again(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    sent = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="repeat-until-ack")

    first = b.client.poll_inbox(wait=0, limit=100)
    assert any(str(item.packet.packet_id) == str(sent.packet_id) for item in first)

    second = b.client.poll_inbox(wait=0, limit=100)
    assert any(str(item.packet.packet_id) == str(sent.packet_id) for item in second)


def test_ack_by_delivery_id_removes_packet(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    sent = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="ack-delivery-id")
    deliveries = b.client.poll_inbox(wait=0, limit=100)
    delivery = next(item for item in deliveries if str(item.packet.packet_id) == str(sent.packet_id))

    result = b.client.ack_delivery(delivery.delivery_id, status="handled")
    assert result["acked"] is True

    after = b.client.poll_inbox(wait=0, limit=100)
    assert all(str(item.packet.packet_id) != str(sent.packet_id) for item in after)


def test_ack_by_packet_id_removes_packet(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    sent = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="ack-packet-id")
    deliveries = b.client.poll_inbox(wait=0, limit=100)
    assert any(str(item.packet.packet_id) == str(sent.packet_id) for item in deliveries)

    result = b.client.ack_packet(sent.packet_id, status="rejected")
    assert result["acked"] is True

    after = b.client.poll_inbox(wait=0, limit=100)
    assert all(str(item.packet.packet_id) != str(sent.packet_id) for item in after)


def test_wrong_node_cannot_ack_delivery(actor_trio):
    a, b, c = actor_trio
    link_bidirectional(a, b)
    sent = a.client.send_packet(from_endpoint_id=a.default_endpoint_id, to_endpoint_id=b.default_endpoint_id, body="ack-isolation")
    deliveries = b.client.poll_inbox(wait=0, limit=100)
    delivery = next(item for item in deliveries if str(item.packet.packet_id) == str(sent.packet_id))

    with pytest.raises(Exception) as exc_info:
        c.client.ack_delivery(delivery.delivery_id)
    assert_http_error(exc_info, 403, "delivery not owned by node")


def test_ack_missing_delivery_rejected(actor_trio):
    _, a, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.ack_delivery("00000000-0000-0000-0000-000000000000")
    assert_http_error(exc_info, 404, "delivery not found")


def test_expired_packet_is_hidden_from_inbox_and_ack(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    sent = a.client.send_packet(
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body="short-lived",
        ttl_seconds=1,
    )

    time.sleep(1.2)

    after = b.client.poll_inbox(wait=0, limit=100)
    assert all(str(item.packet.packet_id) != str(sent.packet_id) for item in after)

    with pytest.raises(Exception) as exc_info:
        b.client.ack_packet(sent.packet_id, status="handled")
    assert_http_error(exc_info, 404, "delivery not found")
