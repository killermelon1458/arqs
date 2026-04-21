from __future__ import annotations

import pytest

from .helpers import assert_http_error, create_endpoint, link_bidirectional, link_mode, raw_json_request


def test_request_and_redeem_bidirectional_link(actor_trio):
    a, b, _ = actor_trio
    code = a.client.request_link_code(a.default_endpoint_id, requested_mode="bidirectional")
    link = b.client.redeem_link_code(code.code, b.default_endpoint_id)
    assert str(link.endpoint_a_id) == a.default_endpoint_id
    assert str(link.endpoint_b_id) == b.default_endpoint_id
    assert link.mode == "bidirectional"
    assert link.status == "active"


def test_redeem_bogus_code_rejected(actor_trio):
    _, b, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        b.client.redeem_link_code("ZZZZZZ", b.default_endpoint_id)
    assert_http_error(exc_info, 404, "link code not found")


def test_redeem_same_code_twice_rejected(actor_trio):
    a, b, c = actor_trio
    code = a.client.request_link_code(a.default_endpoint_id).code
    b.client.redeem_link_code(code, b.default_endpoint_id)
    with pytest.raises(Exception) as exc_info:
        c.client.redeem_link_code(code, c.default_endpoint_id)
    assert_http_error(exc_info, 409, "link code not active")


def test_cannot_self_link_same_endpoint(actor_trio):
    a, _, _ = actor_trio
    code = a.client.request_link_code(a.default_endpoint_id).code
    with pytest.raises(Exception) as exc_info:
        a.client.redeem_link_code(code, a.default_endpoint_id)
    assert_http_error(exc_info, 409, "cannot self-link endpoint")


def test_duplicate_active_link_rejected(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)
    code2 = a.client.request_link_code(a.default_endpoint_id).code
    with pytest.raises(Exception) as exc_info:
        b.client.redeem_link_code(code2, b.default_endpoint_id)
    assert_http_error(exc_info, 409, "duplicate active link")


def test_invalid_link_mode_rejected_via_raw_http(actor_trio):
    a, _, _ = actor_trio
    status, body = raw_json_request(
        "POST",
        "/links/request",
        json_body={"source_endpoint_id": a.default_endpoint_id, "requested_mode": "sideways"},
        headers={"X-ARQS-API-Key": a.api_key},
    )
    assert status == 422 or status == 400


def test_revoke_link_makes_future_send_fail(actor_trio):
    a, b, _ = actor_trio
    _, link = link_bidirectional(a, b)
    revoke_result = a.client.revoke_link(link.link_id)
    assert revoke_result["revoked"] is True

    with pytest.raises(Exception) as exc_info:
        a.client.send_packet(
            from_endpoint_id=a.default_endpoint_id,
            to_endpoint_id=b.default_endpoint_id,
            body="should fail after revoke",
        )
    assert_http_error(exc_info, 403, "no active directed route")


def test_delete_link_not_visible_to_third_party_rejected(actor_trio):
    a, b, c = actor_trio
    _, link = link_bidirectional(a, b)
    with pytest.raises(Exception) as exc_info:
        c.client.revoke_link(link.link_id)
    assert_http_error(exc_info, 403, "link not visible to node")


def test_list_links_only_shows_visible_links(actor_trio):
    a, b, c = actor_trio
    link_bidirectional(a, b)
    links_c = c.client.list_links()
    ids_c = {str(item.link_id) for item in links_c}
    assert ids_c == set()
