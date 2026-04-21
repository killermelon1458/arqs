from __future__ import annotations

import pytest

from .helpers import assert_http_error, link_bidirectional


def test_endpoint_existence_is_leaked_by_403_vs_404(actor_trio):
    a, b, _ = actor_trio

    with pytest.raises(Exception) as foreign_exc:
        a.client.request_link_code(b.default_endpoint_id)
    assert_http_error(foreign_exc, 403, "endpoint not owned by node")

    with pytest.raises(Exception) as missing_exc:
        a.client.request_link_code("00000000-0000-0000-0000-000000000000")
    assert_http_error(missing_exc, 404, "endpoint not found")


def test_link_existence_is_leaked_by_403_vs_404(actor_trio):
    a, b, c = actor_trio
    _, link = link_bidirectional(a, b)

    with pytest.raises(Exception) as foreign_exc:
        c.client.revoke_link(link.link_id)
    assert_http_error(foreign_exc, 403, "link not visible to node")

    with pytest.raises(Exception) as missing_exc:
        c.client.revoke_link("00000000-0000-0000-0000-000000000000")
    assert_http_error(missing_exc, 404, "link not found")


def test_link_code_state_is_leaked_not_found_vs_not_active(actor_trio):
    a, b, c = actor_trio
    code = a.client.request_link_code(a.default_endpoint_id).code
    b.client.redeem_link_code(code, b.default_endpoint_id)

    with pytest.raises(Exception) as used_exc:
        c.client.redeem_link_code(code, c.default_endpoint_id)
    assert_http_error(used_exc, 409, "link code not active")

    with pytest.raises(Exception) as bogus_exc:
        c.client.redeem_link_code("ZZZZZZ", c.default_endpoint_id)
    assert_http_error(bogus_exc, 404, "link code not found")
