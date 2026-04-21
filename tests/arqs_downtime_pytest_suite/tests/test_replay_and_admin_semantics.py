from __future__ import annotations

import pytest

from harness import (
    ack_all,
    assert_single_delivery,
    fixed_packet_payload,
    poll_once,
    revoke_link_any_side,
    send_fixed_packet,
)
from arqs_api import ARQSError, ARQSHTTPError


@pytest.mark.replay
def test_same_packet_id_is_accepted_again_after_final_ack(linked_pair, packet_id):
    sender, receiver = linked_pair
    body = fixed_packet_payload("same-packet-id-after-final-ack")

    first = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert first.result == "accepted"

    first_deliveries = poll_once(receiver, wait=0)
    assert_single_delivery(first_deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, first_deliveries)

    second = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert second.result == "accepted"

    second_deliveries = poll_once(receiver, wait=0)
    assert_single_delivery(second_deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, second_deliveries)


@pytest.mark.admin
def test_revoked_link_does_not_cancel_already_queued_packets(linked_pair):
    sender, receiver = linked_pair
    packet_a = fixed_packet_payload("queued-before-revoke-a")
    packet_b = fixed_packet_payload("queued-before-revoke-b")

    res1 = sender.client.send_packet(
        from_endpoint_id=sender.default_endpoint_id,
        to_endpoint_id=receiver.default_endpoint_id,
        body=packet_a,
        data=None,
        headers={"content_type": "text/plain"},
        meta={"suite": "downtime"},
    )
    res2 = receiver.client.send_packet(
        from_endpoint_id=receiver.default_endpoint_id,
        to_endpoint_id=sender.default_endpoint_id,
        body=packet_b,
        data=None,
        headers={"content_type": "text/plain"},
        meta={"suite": "downtime"},
    )
    assert res1.result == "accepted"
    assert res2.result == "accepted"

    revoke_link_any_side(sender)

    sender_inbox = poll_once(sender, wait=0)
    receiver_inbox = poll_once(receiver, wait=0)

    assert_single_delivery(sender_inbox, body=packet_b)
    assert_single_delivery(receiver_inbox, body=packet_a)
    ack_all(sender, sender_inbox)
    ack_all(receiver, receiver_inbox)

    with pytest.raises(ARQSHTTPError) as exc_info:
        sender.client.send_packet(
            from_endpoint_id=sender.default_endpoint_id,
            to_endpoint_id=receiver.default_endpoint_id,
            body="should-fail-after-revoke",
            data=None,
            headers={"content_type": "text/plain"},
            meta={"suite": "downtime"},
        )
    assert exc_info.value.status_code == 403
    assert "no active directed route" in str(exc_info.value.detail)


@pytest.mark.admin
def test_identity_delete_drops_queued_packets_on_both_sides(linked_pair):
    sender, receiver = linked_pair
    packet_a = fixed_packet_payload("queued-before-identity-delete-a")
    packet_b = fixed_packet_payload("queued-before-identity-delete-b")

    res1 = sender.client.send_packet(
        from_endpoint_id=sender.default_endpoint_id,
        to_endpoint_id=receiver.default_endpoint_id,
        body=packet_a,
        data=None,
        headers={"content_type": "text/plain"},
        meta={"suite": "downtime"},
    )
    res2 = receiver.client.send_packet(
        from_endpoint_id=receiver.default_endpoint_id,
        to_endpoint_id=sender.default_endpoint_id,
        body=packet_b,
        data=None,
        headers={"content_type": "text/plain"},
        meta={"suite": "downtime"},
    )
    assert res1.result == "accepted"
    assert res2.result == "accepted"

    sender_endpoint_id = sender.default_endpoint_id
    sender.client.delete_identity(clear_client_identity=True)

    # The intact side should not receive its queued packet because the peer's
    # identity deletion removes the linked destination objects/state.
    receiver_inbox = poll_once(receiver, wait=0)
    assert receiver_inbox == []

    with pytest.raises(ARQSError):
        poll_once(sender, wait=0)

    with pytest.raises(ARQSHTTPError) as exc_info:
        receiver.client.send_packet(
            from_endpoint_id=receiver.default_endpoint_id,
            to_endpoint_id=sender_endpoint_id,
            body="should-fail-after-identity-delete",
            data=None,
            headers={"content_type": "text/plain"},
            meta={"suite": "downtime"},
        )
    assert exc_info.value.status_code == 404
    assert "destination endpoint not found" in str(exc_info.value.detail)
