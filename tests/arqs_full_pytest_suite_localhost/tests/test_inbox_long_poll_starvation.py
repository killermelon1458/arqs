from __future__ import annotations

import threading
import time
import warnings

import pytest

from .helpers import (
    assert_health_response_schema,
    create_endpoint,
    create_trio,
    link_bidirectional,
    observability_detail,
    raw_json_request,
)


IDLE_WAITERS = 24
LONG_POLL_SECONDS = 5
LONG_POLL_TIMEOUT_SECONDS = 8.0
CRITICAL_TIMEOUT_SECONDS = 2.5
SETTLE_SECONDS = 1.25
RECOVERY_TIMEOUT_SECONDS = 90.0
RECOVERY_POLL_INTERVAL_SECONDS = 0.5


def _start_idle_inbox_waiters(actor, *, num_waiters: int = IDLE_WAITERS):
    barrier = threading.Barrier(num_waiters + 1)
    lock = threading.Lock()
    results: list[tuple[int, int, object]] = []
    errors: list[tuple[int, BaseException]] = []
    headers = {"X-ARQS-API-Key": actor.api_key}
    path = f"/inbox?wait={LONG_POLL_SECONDS}&limit=1"
    threads: list[threading.Thread] = []

    def worker(index: int) -> None:
        try:
            barrier.wait(timeout=10.0)
            status, body = raw_json_request(
                "GET",
                path,
                headers=headers,
                timeout=LONG_POLL_TIMEOUT_SECONDS,
            )
            with lock:
                results.append((index, status, body))
        except BaseException as exc:
            with lock:
                errors.append((index, exc))

    for index in range(num_waiters):
        thread = threading.Thread(target=worker, args=(index,), daemon=True)
        thread.start()
        threads.append(thread)

    barrier.wait(timeout=10.0)
    time.sleep(SETTLE_SECONDS)
    return threads, results, errors


def _join_threads(threads: list[threading.Thread], *, timeout: float) -> None:
    deadline = time.perf_counter() + timeout
    for thread in threads:
        remaining = max(0.0, deadline - time.perf_counter())
        thread.join(timeout=remaining)


def _assert_request_stays_fast(started_at: float, *, operation: str) -> None:
    elapsed = time.perf_counter() - started_at
    assert elapsed < CRITICAL_TIMEOUT_SECONDS, (
        f"{operation} took {elapsed:.2f}s while idle long polls were active; "
        "idle waiters should not starve unrelated requests"
    )


def _new_short_timeout_client(actor):
    client = actor.new_client_from_saved_identity()
    client.timeout = CRITICAL_TIMEOUT_SECONDS
    return client


def _wait_for_server_recovery(*, timeout: float = RECOVERY_TIMEOUT_SECONDS) -> None:
    deadline = time.perf_counter() + timeout
    last_error = "no response received"
    while time.perf_counter() < deadline:
        try:
            status, body = raw_json_request("GET", "/health", timeout=1.0)
            if status == 200:
                assert_health_response_schema(body)
                return
            if status in {401, 403, 404}:
                pytest.skip(
                    "server health observability is not publicly available for starvation tests: "
                    f"HTTP {status} {observability_detail(body)!r}"
                )
            last_error = f"HTTP {status}: {body!r}"
        except BaseException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(RECOVERY_POLL_INTERVAL_SECONDS)

    pytest.fail(
        "server did not recover after long-poll starvation test; "
        f"last health result was {last_error}"
    )


def _check_server_health_once() -> tuple[bool, str]:
    try:
        status, body = raw_json_request("GET", "/health", timeout=1.0)
        if status == 200:
            assert_health_response_schema(body)
            return True, "HTTP 200"
        if status in {401, 403, 404}:
            return False, f"HTTP {status}: health observability unavailable ({observability_detail(body)!r})"
        return False, f"HTTP {status}: {body!r}"
    except BaseException as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _wait_for_server_recovery_quietly(*, timeout: float = RECOVERY_TIMEOUT_SECONDS) -> tuple[bool, str]:
    deadline = time.perf_counter() + timeout
    last_error = "no response received"
    while time.perf_counter() < deadline:
        ok, detail = _check_server_health_once()
        if ok:
            return True, detail
        last_error = detail
        time.sleep(RECOVERY_POLL_INTERVAL_SECONDS)
    return False, last_error


def _create_unique_link(sender, receiver, *, label: str) -> tuple[str, str]:
    sender_endpoint_id = create_endpoint(sender, endpoint_name=f"{label}-sender")
    receiver_endpoint_id = create_endpoint(receiver, endpoint_name=f"{label}-receiver")
    link_bidirectional(
        sender,
        receiver,
        source_endpoint_id=sender_endpoint_id,
        destination_endpoint_id=receiver_endpoint_id,
    )
    return sender_endpoint_id, receiver_endpoint_id


@pytest.fixture(scope="module")
def starvation_actor_trio(tmp_path_factory, server_alive):
    _wait_for_server_recovery()
    base_dir = tmp_path_factory.mktemp("inbox-long-poll-starvation")
    return create_trio(base_dir)


@pytest.fixture(autouse=True)
def guard_server_health_around_test(request):
    ok, detail = _check_server_health_once()
    if not ok:
        pytest.skip(
            f"server is not healthy before {request.node.name}: {detail}. "
            "Restart the server or run this starvation case by itself."
        )
    yield
    ok, detail = _wait_for_server_recovery_quietly()
    if not ok:
        warnings.warn(
            f"server did not recover after {request.node.name}: {detail}. "
            "Later starvation cases in this run will be skipped until the server is restarted or recovers.",
            stacklevel=1,
        )


@pytest.mark.slow
def test_many_idle_long_polls_should_not_block_public_health(starvation_actor_trio):
    _, _, saturator = starvation_actor_trio
    threads, waiter_results, waiter_errors = _start_idle_inbox_waiters(saturator)

    try:
        started = time.perf_counter()
        try:
            status, body = raw_json_request("GET", "/health", timeout=CRITICAL_TIMEOUT_SECONDS)
        except BaseException as exc:  # pragma: no cover - exercised by failing pre-fix servers
            pytest.fail(
                "GET /health timed out or failed while idle long polls were active: "
                f"{type(exc).__name__}: {exc}"
            )

        _assert_request_stays_fast(started, operation="GET /health")
        assert status == 200, f"Expected /health to stay available, got HTTP {status}: {body!r}"
        assert_health_response_schema(body)
    finally:
        _join_threads(threads, timeout=LONG_POLL_TIMEOUT_SECONDS + 2.0)

    assert len(waiter_errors) == 0, f"idle waiter errors: {waiter_errors!r}"
    assert len(waiter_results) == IDLE_WAITERS, f"expected {IDLE_WAITERS} waiter responses, got {len(waiter_results)}"
    assert all(status == 200 for _, status, _ in waiter_results)


@pytest.mark.slow
def test_many_idle_long_polls_should_not_block_packet_send(starvation_actor_trio):
    sender, receiver, saturator = starvation_actor_trio
    sender_endpoint_id, receiver_endpoint_id = _create_unique_link(sender, receiver, label="pool-starvation-send")
    sender_fast = _new_short_timeout_client(sender)
    threads, waiter_results, waiter_errors = _start_idle_inbox_waiters(saturator)
    sent_packet_id = None

    try:
        started = time.perf_counter()
        try:
            send_result = sender_fast.send_packet(
                from_endpoint_id=sender_endpoint_id,
                to_endpoint_id=receiver_endpoint_id,
                body="pool-starvation-send-regression",
            )
        except BaseException as exc:  # pragma: no cover - exercised by failing pre-fix servers
            pytest.fail(
                "POST /packets timed out or failed while idle long polls were active: "
                f"{type(exc).__name__}: {exc}"
            )

        _assert_request_stays_fast(started, operation="POST /packets")
        assert send_result.result == "accepted"
        sent_packet_id = str(send_result.packet_id)
    finally:
        _join_threads(threads, timeout=LONG_POLL_TIMEOUT_SECONDS + 2.0)
        if sent_packet_id is not None:
            try:
                deliveries = receiver.client.poll_inbox(wait=0, limit=100, request_timeout=10.0)
                for delivery in deliveries:
                    if str(delivery.packet.packet_id) == sent_packet_id:
                        receiver.client.ack_delivery(delivery.delivery_id, status="handled")
                        break
            except Exception:
                pass

    assert len(waiter_errors) == 0, f"idle waiter errors: {waiter_errors!r}"
    assert len(waiter_results) == IDLE_WAITERS, f"expected {IDLE_WAITERS} waiter responses, got {len(waiter_results)}"


@pytest.mark.slow
def test_many_idle_long_polls_should_not_block_packet_ack(starvation_actor_trio):
    sender, receiver, saturator = starvation_actor_trio
    sender_endpoint_id, receiver_endpoint_id = _create_unique_link(sender, receiver, label="pool-starvation-ack")
    receiver_fast = _new_short_timeout_client(receiver)
    sent = sender.client.send_packet(
        from_endpoint_id=sender_endpoint_id,
        to_endpoint_id=receiver_endpoint_id,
        body="pool-starvation-ack-regression",
    )
    deliveries = receiver.client.poll_inbox(wait=0, limit=100, request_timeout=10.0)
    delivery = next(item for item in deliveries if str(item.packet.packet_id) == str(sent.packet_id))
    threads, waiter_results, waiter_errors = _start_idle_inbox_waiters(saturator)

    try:
        started = time.perf_counter()
        try:
            ack_result = receiver_fast.ack_delivery(delivery.delivery_id, status="handled")
        except BaseException as exc:  # pragma: no cover - exercised by failing pre-fix servers
            pytest.fail(
                "POST /packet_ack timed out or failed while idle long polls were active: "
                f"{type(exc).__name__}: {exc}"
            )

        _assert_request_stays_fast(started, operation="POST /packet_ack")
        assert ack_result["acked"] is True
    finally:
        _join_threads(threads, timeout=LONG_POLL_TIMEOUT_SECONDS + 2.0)
        try:
            receiver.client.ack_delivery(delivery.delivery_id, status="handled")
        except Exception:
            pass

    assert len(waiter_errors) == 0, f"idle waiter errors: {waiter_errors!r}"
    assert len(waiter_results) == IDLE_WAITERS, f"expected {IDLE_WAITERS} waiter responses, got {len(waiter_results)}"
