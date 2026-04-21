from __future__ import annotations

import pytest

from arqs_api import ARQSClient

from .helpers import Actor, assert_http_error, raw_json_request, unique_name


def test_missing_api_key_is_401(server_alive):
    status, body = raw_json_request("GET", "/endpoints")
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("detail") == "missing api key"


def test_invalid_api_key_is_401(server_alive):
    status, body = raw_json_request("GET", "/endpoints", headers={"X-ARQS-API-Key": "arqs_invalid_key"})
    assert status == 401
    assert isinstance(body, dict)
    assert body.get("detail") == "invalid api key"


def test_authorization_bearer_header_authenticates(tmp_path, server_alive):
    actor = Actor(unique_name("bearer-auth"), tmp_path / "bearer").initialize()
    status, body = raw_json_request("GET", "/endpoints", headers={"Authorization": f"Bearer {actor.api_key}"})
    assert status == 200
    assert isinstance(body, list)
    assert any(item["endpoint_id"] == actor.default_endpoint_id for item in body)


def test_rotate_key_invalidates_old_key(tmp_path, server_alive):
    actor = Actor(unique_name("rotate"), tmp_path / "rotate").initialize()
    old_key = actor.api_key
    rotated = actor.client.rotate_key(update_client_key=True)

    status_old, body_old = raw_json_request("GET", "/endpoints", headers={"X-ARQS-API-Key": old_key})
    assert status_old == 401
    assert body_old.get("detail") == "invalid api key"

    status_new, body_new = raw_json_request("GET", "/endpoints", headers={"X-ARQS-API-Key": rotated.api_key})
    assert status_new == 200
    assert isinstance(body_new, list)


def test_protected_endpoint_rejects_bad_bearer(server_alive):
    status, body = raw_json_request("GET", "/endpoints", headers={"Authorization": "Bearer arqs_bad"})
    assert status == 401
    assert body.get("detail") == "invalid api key"
