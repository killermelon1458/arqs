# test_bandwidth_limits.py
from __future__ import annotations

import json
import pytest

from .helpers import assert_http_error, link_bidirectional

MAX_PACKET_BYTES = 262144


def _payload_size_bytes(*, headers: dict, body: str | None, data: dict, meta: dict) -> int:
    envelope = {
        "headers": headers,
        "body": body,
        "data": data,
        "meta": meta,
    }
    return len(json.dumps(envelope, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _max_body_for_limit(limit: int, *, headers: dict, data: dict, meta: dict) -> str:
    lo = 0
    hi = limit

    while lo < hi:
        mid = (lo + hi + 1) // 2
        candidate = "X" * mid
        size = _payload_size_bytes(headers=headers, body=candidate, data=data, meta=meta)
        if size <= limit:
            lo = mid
        else:
            hi = mid - 1

    return "X" * lo


def test_max_packet_bytes_boundary_and_overflow(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)

    headers = {"content_type": "text/plain"}
    data = {}
    meta = {"suite": "bandwidth-boundary"}

    body_at_limit = _max_body_for_limit(
        MAX_PACKET_BYTES,
        headers=headers,
        data=data,
        meta=meta,
    )

    exact_size = _payload_size_bytes(
        headers=headers,
        body=body_at_limit,
        data=data,
        meta=meta,
    )
    overflow_size = _payload_size_bytes(
        headers=headers,
        body=body_at_limit + "X",
        data=data,
        meta=meta,
    )

    assert exact_size <= MAX_PACKET_BYTES
    assert overflow_size > MAX_PACKET_BYTES

    accepted = a.client.send_packet(
        from_endpoint_id=a.default_endpoint_id,
        to_endpoint_id=b.default_endpoint_id,
        body=body_at_limit,
        data=data,
        headers=headers,
        meta=meta,
    )
    assert accepted.result == "accepted"

    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body=body_at_limit + "X",
            data=data,
            headers=headers,
            meta=meta,
        )

    assert_http_error(exc_info, 400, "packet exceeds max packet bytes")