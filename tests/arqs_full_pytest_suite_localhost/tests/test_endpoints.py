from __future__ import annotations

import pytest

from .helpers import assert_http_error, create_endpoint, raw_json_request


def test_list_endpoints_only_shows_own(actor_trio):
    a, b, _ = actor_trio
    a_extra = create_endpoint(a, endpoint_name="a-extra")
    b_extra = create_endpoint(b, endpoint_name="b-extra")

    rows_a = a.client.list_endpoints()
    ids_a = {str(item.endpoint_id) for item in rows_a}
    assert a.default_endpoint_id in ids_a
    assert a_extra in ids_a
    assert b.default_endpoint_id not in ids_a
    assert b_extra not in ids_a


def test_create_endpoint_succeeds(actor_trio):
    a, _, _ = actor_trio
    endpoint_id = create_endpoint(a, endpoint_name="created-by-pytest")
    ids = {str(item.endpoint_id) for item in a.client.list_endpoints()}
    assert endpoint_id in ids


def test_delete_default_endpoint_rejected(actor_trio):
    a, _, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.delete_endpoint(a.default_endpoint_id)
    assert_http_error(exc_info, 409, "default endpoint deletion not allowed")


def test_delete_foreign_endpoint_rejected(actor_trio):
    a, b, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.delete_endpoint(b.default_endpoint_id)
    assert_http_error(exc_info, 403, "endpoint not owned by node")


def test_request_link_code_on_foreign_endpoint_rejected(actor_trio):
    a, b, _ = actor_trio
    with pytest.raises(Exception) as exc_info:
        a.client.request_link_code(b.default_endpoint_id)
    assert_http_error(exc_info, 403, "endpoint not owned by node")


def test_delete_unlinked_nondefault_endpoint_succeeds(actor_trio):
    a, _, _ = actor_trio
    endpoint_id = create_endpoint(a, endpoint_name="delete-me")
    result = a.client.delete_endpoint(endpoint_id)
    assert result["deleted"] is True
    ids = {str(item.endpoint_id) for item in a.client.list_endpoints()}
    assert endpoint_id not in ids
