from __future__ import annotations

import time
from pathlib import Path

import pytest

from harness import (
    ack_all,
    assert_single_delivery,
    docker_restart_hard,
    fixed_packet_payload,
    launch_poll_worker,
    poll_once,
    send_fixed_packet,
)


@pytest.mark.recovery
def test_redelivery_after_receiver_worker_crash_before_ack_and_restart(linked_pair, packet_id, tmp_path: Path):
    sender, receiver = linked_pair
    body = fixed_packet_payload("receiver-crash-before-ack")

    result = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert result.result == "accepted"

    first_output = tmp_path / "first_poll.json"
    first_checkpoint = tmp_path / "first_received.chk"
    worker1 = launch_poll_worker(
        identity_path=receiver.identity_path,
        output_path=first_output,
        checkpoint_path=first_checkpoint,
        wait_seconds=5,
        ack=False,
        hold_after_receive=True,
    )
    try:
        worker1.wait_for_checkpoint(timeout_seconds=15)
        first_payload = worker1.read_output_json()
        assert first_payload["status"] == "ok"
        assert first_payload["count"] == 1
        assert first_payload["deliveries"][0]["packet_id"] == packet_id
    finally:
        worker1.kill()

    second_output = tmp_path / "second_poll.json"
    worker2 = launch_poll_worker(
        identity_path=receiver.identity_path,
        output_path=second_output,
        wait_seconds=5,
        ack=False,
        hold_after_receive=False,
    )
    worker2.wait(timeout_seconds=20)
    second_payload = worker2.read_output_json()
    assert second_payload["status"] == "ok"
    assert second_payload["count"] == 1
    assert second_payload["deliveries"][0]["packet_id"] == packet_id
    assert second_payload["deliveries"][0]["body"] == body

    deliveries = poll_once(receiver, wait=0)
    delivery = assert_single_delivery(deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, deliveries)
    assert str(delivery.packet.packet_id) == packet_id


@pytest.mark.recovery
@pytest.mark.docker
def test_long_poll_worker_can_recover_after_server_restart(linked_pair, packet_id, tmp_path: Path):
    sender, receiver = linked_pair

    first_output = tmp_path / "long_poll_error.json"
    worker1 = launch_poll_worker(
        identity_path=receiver.identity_path,
        output_path=first_output,
        wait_seconds=20,
        ack=False,
        hold_after_receive=False,
    )

    time.sleep(1.5)
    docker_restart_hard()
    worker1.wait(timeout_seconds=30)
    first_payload = worker1.read_output_json()
    assert first_payload["status"] in {"ok", "error"}

    body = fixed_packet_payload("long-poll-recovery")
    result = send_fixed_packet(sender, receiver, packet_id=packet_id, body=body)
    assert result.result == "accepted"

    second_output = tmp_path / "post_restart_poll.json"
    worker2 = launch_poll_worker(
        identity_path=receiver.identity_path,
        output_path=second_output,
        wait_seconds=5,
        ack=False,
        hold_after_receive=False,
    )
    worker2.wait(timeout_seconds=20)
    second_payload = worker2.read_output_json()
    assert second_payload["status"] == "ok"
    assert second_payload["count"] == 1
    assert second_payload["deliveries"][0]["packet_id"] == packet_id
    assert second_payload["deliveries"][0]["body"] == body

    deliveries = poll_once(receiver, wait=0)
    delivery = assert_single_delivery(deliveries, packet_id=packet_id, body=body)
    ack_all(receiver, deliveries)
    assert str(delivery.packet.packet_id) == packet_id
