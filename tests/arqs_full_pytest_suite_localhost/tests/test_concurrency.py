from __future__ import annotations

import pytest
import threading
import uuid

from .helpers import assert_http_error, concurrent_call, create_endpoint, link_bidirectional


def test_redeem_race_only_one_wins(actor_trio):
    a, b, c = actor_trio
    code = a.client.request_link_code(a.default_endpoint_id).code

    results, errors = concurrent_call(
        lambda: b.client.redeem_link_code(code, b.default_endpoint_id),
        lambda: c.client.redeem_link_code(code, c.default_endpoint_id),
    )

    assert len(results) == 1
    assert len(errors) == 1
    err = errors[0]
    assert getattr(err, "status_code", None) == 409
    assert "link code not active" in str(getattr(err, "detail", err))

@pytest.mark.slow
def test_redeem_race_repeated_no_double_wins(actor_trio):
    a, b, c = actor_trio

    for i in range(25):
        source_endpoint_id = create_endpoint(a, endpoint_name=f"race-src-{i}")
        code = a.client.request_link_code(source_endpoint_id).code

        barrier = threading.Barrier(3)
        results = []
        errors = []
        lock = threading.Lock()

        def redeem(client, destination_endpoint_id):
            try:
                barrier.wait(timeout=5)
                result = client.client.redeem_link_code(code, destination_endpoint_id)
                with lock:
                    results.append(result)
            except BaseException as exc:
                with lock:
                    errors.append(exc)

        t1 = threading.Thread(
            target=redeem,
            args=(b, b.default_endpoint_id),
            daemon=True,
        )
        t2 = threading.Thread(
            target=redeem,
            args=(c, c.default_endpoint_id),
            daemon=True,
        )

        t1.start()
        t2.start()

        # Release both worker threads at the same time.
        barrier.wait(timeout=5)

        t1.join()
        t2.join()

        assert len(results) == 1, f"round {i}: expected exactly 1 redeem success, got {len(results)}"
        assert len(errors) == 1, f"round {i}: expected exactly 1 redeem failure, got {len(errors)}"

        err = errors[0]
        assert getattr(err, "status_code", None) == 409, (
            f"round {i}: expected HTTP 409 loser, got {type(err).__name__}: {err}"
        )
        assert "link code not active" in str(getattr(err, "detail", err)), (
            f"round {i}: expected loser detail to mention inactive code, got {err}"
        )

def test_duplicate_packet_race_same_payload_one_accepted_one_duplicate(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    packet_id = str(uuid.uuid4())

    results, errors = concurrent_call(
        lambda: a.client.send_packet(
            packet_id=packet_id,
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body="race-payload",
        ),
        lambda: a.client.send_packet(
            packet_id=packet_id,
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body="race-payload",
        ),
    )

    assert len(errors) == 0
    assert len(results) == 2
    states = sorted(result.result for result in results)
    assert states == ["accepted", "duplicate"]
