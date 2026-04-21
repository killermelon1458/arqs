from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path
import tempfile

import pytest

from arqs_api import ARQSHTTPError

from .helpers import BASE_URL, create_trio, link_bidirectional, raw_json_request


def _auth_headers(api_key: str) -> dict[str, str]:
    return {"X-ARQS-API-Key": api_key}


@pytest.mark.slow
def test_repeated_overlapping_poll_and_ack_should_not_trigger_inbox_500():
    health_status, health_body = raw_json_request("GET", "/health")
    assert health_status == 200, f"Server at {BASE_URL} did not answer /health with 200; got {health_status} / {health_body!r}"

    workspace_tmp_root = Path(__file__).resolve().parents[1] / "local_tmp"
    workspace_tmp_root.mkdir(parents=True, exist_ok=True)
    test_dir = Path(tempfile.mkdtemp(prefix="poll-write-race-", dir=workspace_tmp_root))

    try:
        sender, receiver, _ = create_trio(test_dir)
        link_bidirectional(sender, receiver)

        receiver_headers = _auth_headers(receiver.api_key)
        poll_path = "/inbox?wait=0&limit=100"
        parallel_polls = 6
        rounds = 30
        reproduction = None

        for round_index in range(rounds):
            sent = sender.client.send_packet(
                from_endpoint_id=sender.default_endpoint_id,
                to_endpoint_id=receiver.default_endpoint_id,
                body=f"poll-race-round-{round_index}",
            )

            first_poll = receiver.client.poll_inbox(wait=0, limit=100)
            delivery = next(
                item
                for item in first_poll
                if str(item.packet.packet_id) == str(sent.packet_id)
            )

            barrier = threading.Barrier(parallel_polls + 2)
            lock = threading.Lock()
            poll_statuses: list[int | str] = []
            poll_bodies: list[object] = []
            ack_result: dict[str, object] = {}

            def poller():
                try:
                    barrier.wait(timeout=5)
                    status_code, body = raw_json_request(
                        "GET",
                        poll_path,
                        headers=receiver_headers,
                        timeout=10.0,
                    )
                    with lock:
                        poll_statuses.append(status_code)
                        poll_bodies.append(body)
                except BaseException as exc:
                    with lock:
                        poll_statuses.append(f"thread-error:{type(exc).__name__}")
                        poll_bodies.append(repr(exc))

            def acker():
                try:
                    barrier.wait(timeout=5)
                    # Give the pollers a brief head start so they are more likely to
                    # overlap with the delete-on-ack path.
                    time.sleep(0.02)
                    status_code, body = raw_json_request(
                        "POST",
                        "/packet_ack",
                        headers=receiver_headers,
                        json_body={"delivery_id": str(delivery.delivery_id), "status": "handled"},
                        timeout=10.0,
                    )
                    with lock:
                        ack_result["status_code"] = status_code
                        ack_result["body"] = body
                except BaseException as exc:
                    with lock:
                        ack_result["status_code"] = f"thread-error:{type(exc).__name__}"
                        ack_result["body"] = repr(exc)

            threads = [
                threading.Thread(target=poller, daemon=True)
                for _ in range(parallel_polls)
            ]
            ack_thread = threading.Thread(target=acker, daemon=True)

            for thread in threads:
                thread.start()
            ack_thread.start()

            barrier.wait(timeout=5)

            for thread in threads:
                thread.join(timeout=10)
            ack_thread.join(timeout=10)

            assert all(not thread.is_alive() for thread in threads), f"round {round_index}: poll threads did not finish"
            assert not ack_thread.is_alive(), f"round {round_index}: ack thread did not finish"

            if any(status == 500 for status in poll_statuses):
                reproduction = {
                    "round": round_index,
                    "poll_statuses": poll_statuses,
                    "poll_bodies": poll_bodies,
                    "ack_result": ack_result,
                    "packet_id": str(sent.packet_id),
                    "delivery_id": str(delivery.delivery_id),
                }
                break

            if ack_result.get("status_code") != 200:
                try:
                    receiver.client.ack_packet(sent.packet_id, status="handled")
                except ARQSHTTPError as exc:
                    assert exc.status_code == 404, (
                        f"round {round_index}: unexpected cleanup ack failure "
                        f"{exc.status_code} {exc.detail}"
                    )

        assert reproduction is None, (
            "reproduced the overlapping /inbox write race before the server patch: "
            f"{reproduction}"
        )
    finally:
        shutil.rmtree(test_dir, ignore_errors=True)
