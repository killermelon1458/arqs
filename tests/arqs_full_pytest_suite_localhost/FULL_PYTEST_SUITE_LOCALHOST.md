# ARQS Full Pytest Suite Localhost

This document covers the `tests/arqs_full_pytest_suite_localhost` suite from `testsv0.1.5.dev.zip` and its actual test files, markers, and run options.

## Run commands for the full suite

These examples assume you are in:

```bash
cd tests/arqs_full_pytest_suite_localhost
```

### Default run

```bash
pytest -q
```

Runs the default localhost suite only. This includes the normal black-box validation tests and skips anything marked `slow` or `compromise`.

### Include slow tests

```bash
pytest -q --run-slow
```

Runs the default suite plus tests marked `slow`. In this suite, that means the noisier localhost-safe abuse checks, including repeated invalid redeem attempts and the send-rate-limit test.

### Include compromise tests

```bash
pytest -q --run-compromise
```

Runs the default suite plus tests marked `compromise`. In this suite, that means the shared-identity / local identity theft simulation.

### Run everything

```bash
pytest --run-slow --run-compromise
```

Runs every test in the suite.

### Override server URL

```bash
ARQS_BASE_URL=http://localhost:8080 pytest -q
```

Targets a different ARQS server URL instead of the suite default. The suite default is `http://localhost:8080`.

### Run one test file

```bash
pytest -q tests/test_links.py
```

Runs only one test module.

### Run one specific test

```bash
pytest -q tests/test_links.py::test_request_and_redeem_bidirectional_link
```

Runs exactly one test function.

### Run one slow test

```bash
pytest -q --run-slow tests/test_limits_and_abuse.py::test_invalid_redeem_burst_stays_rejected
```

Runs exactly one test that is marked `slow`.

### Run one compromise test

```bash
pytest -q --run-compromise tests/test_limits_and_abuse.py::test_local_identity_theft_via_shared_identity_file
```

Runs exactly one test that is marked `compromise`.

### Run the rate-limit recovery test correctly

```bash
pytest -q --run-slow tests/test_limits_and_abuse.py::test_send_rate_limit_enforced_and_recovers
```

This test uses the live admin CLI against the running Dockerized server. It reads the current send-rate settings, temporarily sets a small deterministic test limit, runs the assertions, and then restores the original values.

## What the suite covers

- Public endpoint exposure checks for `/health` and `/register`
- Config-sensitive observability handling for `/stats`
- Authentication behavior and key rotation
- Endpoint ownership, creation, and deletion rules
- Link-code lifecycle, duplicate prevention, and visibility
- Directed-route behavior for packets
- Packet duplicate/idempotency handling
- Inbox polling and ACK ownership rules
- Concurrency races
- Packet-size boundary checks
- Optional localhost abuse checks
- Optional shared-identity compromise simulation

## Per-test reference

---

## `tests/test_public_endpoints.py`

### `test_health_is_public`
Checks that `/health` is public and returns the minimal status structure: `status` and `time`, without `app` or `db_path`.

Run only this test:

```bash
pytest -q tests/test_public_endpoints.py::test_health_is_public
```

### `test_stats_is_currently_public`
Checks that `/stats` returns aggregate stats plus `time` when public, and skips cleanly when stats observability is protected or disabled.

Run only this test:

```bash
pytest -q tests/test_public_endpoints.py::test_stats_is_currently_public
```

### `test_register_is_currently_public`
Checks that `/register` is currently public and returns a node ID, API key, and default endpoint ID.

Run only this test:

```bash
pytest -q tests/test_public_endpoints.py::test_register_is_currently_public
```

---

## `tests/test_authentication.py`

### `test_missing_api_key_is_401`
Checks that calling a protected endpoint without an API key returns HTTP 401 with `missing api key`.

Run only this test:

```bash
pytest -q tests/test_authentication.py::test_missing_api_key_is_401
```

### `test_invalid_api_key_is_401`
Checks that a bad API key returns HTTP 401 with `invalid api key`.

Run only this test:

```bash
pytest -q tests/test_authentication.py::test_invalid_api_key_is_401
```

### `test_authorization_bearer_header_authenticates`
Checks that bearer-token auth works when the API key is sent in the `Authorization: Bearer ...` header.

Run only this test:

```bash
pytest -q tests/test_authentication.py::test_authorization_bearer_header_authenticates
```

### `test_rotate_key_invalidates_old_key`
Checks that rotating a node key invalidates the old key and that the new key works immediately.

Run only this test:

```bash
pytest -q tests/test_authentication.py::test_rotate_key_invalidates_old_key
```

### `test_protected_endpoint_rejects_bad_bearer`
Checks that a bad bearer token is rejected as invalid auth.

Run only this test:

```bash
pytest -q tests/test_authentication.py::test_protected_endpoint_rejects_bad_bearer
```

---

## `tests/test_endpoints.py`

### `test_list_endpoints_only_shows_own`
Checks that a node only sees its own endpoints, not another node’s endpoints.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_list_endpoints_only_shows_own
```

### `test_create_endpoint_succeeds`
Checks that creating a new endpoint succeeds and the endpoint appears in that node’s endpoint list.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_create_endpoint_succeeds
```

### `test_delete_default_endpoint_rejected`
Checks that deleting the default endpoint is rejected.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_delete_default_endpoint_rejected
```

### `test_delete_foreign_endpoint_rejected`
Checks that a node cannot delete an endpoint it does not own.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_delete_foreign_endpoint_rejected
```

### `test_request_link_code_on_foreign_endpoint_rejected`
Checks that a node cannot request a link code from another node’s endpoint.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_request_link_code_on_foreign_endpoint_rejected
```

### `test_delete_unlinked_nondefault_endpoint_succeeds`
Checks that a non-default endpoint can be deleted if it is unlinked.

Run only this test:

```bash
pytest -q tests/test_endpoints.py::test_delete_unlinked_nondefault_endpoint_succeeds
```

---

## `tests/test_links.py`

### `test_request_and_redeem_bidirectional_link`
Checks the normal bidirectional link flow: request link code, redeem it, verify the link becomes active.

Run only this test:

```bash
pytest -q tests/test_links.py::test_request_and_redeem_bidirectional_link
```

### `test_redeem_bogus_code_rejected`
Checks that redeeming a fake link code returns the expected rejection.

Run only this test:

```bash
pytest -q tests/test_links.py::test_redeem_bogus_code_rejected
```

### `test_redeem_same_code_twice_rejected`
Checks that a link code is single-use and cannot be redeemed twice.

Run only this test:

```bash
pytest -q tests/test_links.py::test_redeem_same_code_twice_rejected
```

### `test_cannot_self_link_same_endpoint`
Checks that the same endpoint cannot link to itself.

Run only this test:

```bash
pytest -q tests/test_links.py::test_cannot_self_link_same_endpoint
```

### `test_duplicate_active_link_rejected`
Checks that a second active duplicate link between the same endpoints is rejected.

Run only this test:

```bash
pytest -q tests/test_links.py::test_duplicate_active_link_rejected
```

### `test_invalid_link_mode_rejected_via_raw_http`
Checks that an invalid `requested_mode` is rejected at the HTTP layer.

Run only this test:

```bash
pytest -q tests/test_links.py::test_invalid_link_mode_rejected_via_raw_http
```

### `test_revoke_link_makes_future_send_fail`
Checks that once a link is revoked, future sends over that route fail.

Run only this test:

```bash
pytest -q tests/test_links.py::test_revoke_link_makes_future_send_fail
```

### `test_delete_link_not_visible_to_third_party_rejected`
Checks that a third party cannot revoke a link it cannot see.

Run only this test:

```bash
pytest -q tests/test_links.py::test_delete_link_not_visible_to_third_party_rejected
```

### `test_list_links_only_shows_visible_links`
Checks that link listing only returns links visible to that node.

Run only this test:

```bash
pytest -q tests/test_links.py::test_list_links_only_shows_visible_links
```

---

## `tests/test_packets.py`

### `test_send_requires_active_route`
Checks that packet send is rejected if there is no active directed route.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_send_requires_active_route
```

### `test_bidirectional_route_allows_send_both_ways`
Checks that a bidirectional link allows packet send in both directions.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_bidirectional_route_allows_send_both_ways
```

### `test_one_way_a_to_b_blocks_reverse`
Checks that an `a_to_b` link allows A→B traffic but blocks B→A traffic.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_one_way_a_to_b_blocks_reverse
```

### `test_send_from_foreign_endpoint_rejected`
Checks that a node cannot forge the sender by using another node’s endpoint as `from_endpoint_id`.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_send_from_foreign_endpoint_rejected
```

### `test_send_to_nonexistent_endpoint_rejected`
Checks that sending to a nonexistent destination endpoint returns the expected rejection.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_send_to_nonexistent_endpoint_rejected
```

### `test_duplicate_packet_id_same_payload_returns_duplicate`
Checks idempotency behavior: reusing the same `packet_id` with the same payload returns `duplicate`, not a second accepted packet.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_duplicate_packet_id_same_payload_returns_duplicate
```

### `test_duplicate_packet_id_different_payload_conflicts`
Checks that reusing a `packet_id` with a different payload is rejected as a conflict.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_duplicate_packet_id_different_payload_conflicts
```

### `test_send_requires_body_or_data`
Checks the client-side validation that at least one of `body` or non-empty `data` must be present.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_send_requires_body_or_data
```

### `test_oversize_packet_rejected`
Checks that an oversized packet is rejected by the server.

Run only this test:

```bash
pytest -q tests/test_packets.py::test_oversize_packet_rejected
```

---

## `tests/test_inbox_ack.py`

### `test_inbox_only_returns_own_deliveries`
Checks inbox isolation so a node only receives deliveries addressed to its own endpoints.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_inbox_only_returns_own_deliveries
```

### `test_poll_without_ack_returns_same_delivery_again`
Checks that unacked deliveries remain available and are returned again on later polls.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_poll_without_ack_returns_same_delivery_again
```

### `test_ack_by_delivery_id_removes_packet`
Checks that acknowledging by `delivery_id` removes the queued delivery.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_ack_by_delivery_id_removes_packet
```

### `test_ack_by_packet_id_removes_packet`
Checks that acknowledging by `packet_id` removes the queued delivery.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_ack_by_packet_id_removes_packet
```

### `test_wrong_node_cannot_ack_delivery`
Checks that a different node cannot ACK someone else’s delivery.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_wrong_node_cannot_ack_delivery
```

### `test_ack_missing_delivery_rejected`
Checks that ACKing a nonexistent delivery is rejected.

Run only this test:

```bash
pytest -q tests/test_inbox_ack.py::test_ack_missing_delivery_rejected
```

---

## `tests/test_concurrency.py`

### `test_redeem_race_only_one_wins`
Checks a two-client redeem race and verifies only one redeem succeeds.

Run only this test:

```bash
pytest -q tests/test_concurrency.py::test_redeem_race_only_one_wins
```

### `test_redeem_race_repeated_no_double_wins`
Repeats the redeem race many times to make sure the server never allows two winners.

Run only this test:

```bash
pytest -q tests/test_concurrency.py::test_redeem_race_repeated_no_double_wins
```

### `test_duplicate_packet_race_same_payload_one_accepted_one_duplicate`
Checks that concurrent sends with the same `packet_id` and same payload result in one `accepted` and one `duplicate`.

Run only this test:

```bash
pytest -q tests/test_concurrency.py::test_duplicate_packet_race_same_payload_one_accepted_one_duplicate
```

---

## `tests/test_bandwidth_limits.py`

### `test_max_packet_bytes_boundary_and_overflow`
Checks the packet-size boundary exactly at the configured maximum and verifies overflow is rejected.

Run only this test:

```bash
pytest -q tests/test_bandwidth_limits.py::test_max_packet_bytes_boundary_and_overflow
```

---

## `tests/test_identity_delete.py`

### `test_delete_identity_invalidates_api_key_and_clears_client`
Checks that deleting an identity removes server-side auth validity and clears the client’s loaded identity.

Run only this test:

```bash
pytest -q tests/test_identity_delete.py::test_delete_identity_invalidates_api_key_and_clears_client
```

### `test_delete_identity_cascades_links_routes_codes_packets_and_deliveries`
Checks that identity deletion cascades through linked server-side resources, including routes, codes, packets, and queued deliveries.

Run only this test:

```bash
pytest -q tests/test_identity_delete.py::test_delete_identity_cascades_links_routes_codes_packets_and_deliveries
```

---

## `tests/test_visibility_and_enumeration.py`

### `test_endpoint_existence_is_leaked_by_403_vs_404`
Checks whether endpoint existence can be inferred by the difference between `403` and `404` responses.

Run only this test:

```bash
pytest -q tests/test_visibility_and_enumeration.py::test_endpoint_existence_is_leaked_by_403_vs_404
```

### `test_link_existence_is_leaked_by_403_vs_404`
Checks whether link existence can be inferred by the difference between `403` and `404` responses.

Run only this test:

```bash
pytest -q tests/test_visibility_and_enumeration.py::test_link_existence_is_leaked_by_403_vs_404
```

### `test_link_code_state_is_leaked_not_found_vs_not_active`
Checks whether link-code state leaks through different responses for nonexistent versus already-used / inactive codes.

Run only this test:

```bash
pytest -q tests/test_visibility_and_enumeration.py::test_link_code_state_is_leaked_not_found_vs_not_active
```

---

## `tests/test_limits_and_abuse.py`

This file contains optional tests. Two are marked `slow`. One is marked `compromise`.

### `test_invalid_redeem_burst_stays_rejected`
Repeatedly sends invalid redeem attempts and checks that they stay rejected under repetition.

Run only this test:

```bash
pytest -q --run-slow tests/test_limits_and_abuse.py::test_invalid_redeem_burst_stays_rejected
```

### `test_send_rate_limit_enforced_and_recovers`
Checks that the live send-rate limit triggers and then recovers after the window expires.

This test no longer depends on `ARQS_EXPECT_*` environment variables.

It uses `docker compose exec` to call `python -m app.admin_cli rate show/set --json` against the running `arqs-server` container, temporarily lowers the rate limit for the test, and restores the original values in cleanup.

Run only this test:

```bash
pytest -q --run-slow tests/test_limits_and_abuse.py::test_send_rate_limit_enforced_and_recovers
```

### `test_local_identity_theft_via_shared_identity_file`
Simulates a local/shared-storage compromise where another client loads a victim’s saved identity file and successfully acts as that victim.

Run only this test:

```bash
pytest -q --run-compromise tests/test_limits_and_abuse.py::test_local_identity_theft_via_shared_identity_file
```

## Useful grouped runs

### Run just the public-surface tests

```bash
pytest -q tests/test_public_endpoints.py
```

### Run just the auth tests

```bash
pytest -q tests/test_authentication.py
```

### Run just endpoint and link ownership tests

```bash
pytest -q tests/test_endpoints.py tests/test_links.py
```

### Run just routing and packet behavior tests

```bash
pytest -q tests/test_packets.py tests/test_inbox_ack.py
```

### Run just concurrency tests

```bash
pytest -q tests/test_concurrency.py
```

### Run just the optional abuse tests

```bash
pytest -q --run-slow tests/test_limits_and_abuse.py
```

### Run just the optional compromise test

```bash
pytest -q --run-compromise tests/test_limits_and_abuse.py::test_local_identity_theft_via_shared_identity_file
```

## Notes

- The suite is black-box against a running server.
- It does **not** assume an empty database.
- The suite vendors its own `arqs_api.py` copy.
- The default base URL is `http://localhost:8080` unless `ARQS_BASE_URL` is set.
- `slow` tests require `--run-slow`.
- `compromise` tests require `--run-compromise`.
