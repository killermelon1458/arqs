from __future__ import annotations

import time

import pytest

from harness import (
    ack_all,
    assert_single_delivery,
    docker_kill_hard,
    docker_restart_graceful,
    docker_restart_hard,
    docker_start,
    fixed_packet_payload,
    poll_once,
    send_fixed_packet,
)


@pytest.mark.recovery
@pytest.mark.docker
def test_packet_survives_graceful_server_restart_before_first_pull(linked_pair, packet_id):
    sender, receiver = linked_pair
    body = fixed_packet_payload("graceful-restart-before-pull")

    result = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert result.result == "accepted"

    docker_restart_graceful()

    deliveries = poll_once(receiver, wait=0)
    delivery = assert_single_delivery(deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, deliveries)
    assert str(delivery.packet.packet_id) == packet_id


@pytest.mark.recovery
@pytest.mark.docker
def test_packet_survives_hard_server_kill_before_first_pull(linked_pair, packet_id):
    sender, receiver = linked_pair
    body = fixed_packet_payload("hard-kill-before-pull")

    result = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert result.result == "accepted"

    docker_restart_hard()

    deliveries = poll_once(receiver, wait=0)
    delivery = assert_single_delivery(deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, deliveries)
    assert str(delivery.packet.packet_id) == packet_id


@pytest.mark.recovery
@pytest.mark.docker
def test_packet_expires_while_server_is_down(linked_pair, packet_id):
    sender, receiver = linked_pair
    body = fixed_packet_payload("ttl-expires-while-down")

    result = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body, ttl_seconds=2)
    assert result.result == "accepted"

    docker_kill_hard()
    time.sleep(5.0)
    docker_start()

    deliveries = poll_once(receiver, wait=0)
    assert deliveries == []
