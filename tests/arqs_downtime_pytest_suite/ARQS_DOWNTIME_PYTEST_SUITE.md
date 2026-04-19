# ARQS_DOWNTIME_PYTEST_SUITE

## Purpose

This suite tests ARQS behavior across downtime, restart, and recovery conditions.

It focuses on:

* queued message persistence
* server restart recovery
* hard-kill recovery
* client recovery after restart
* replay behavior after final ACK
* revoke/delete semantics during queued delivery
* TTL expiration while the server is down

## Change to the correct directory first

From the repo root:

```powershell
cd tests/arqs_downtime_pytest_suite
```

Everything below assumes you are already in:

```text
tests/arqs_downtime_pytest_suite
```

## Run Commands

### Run the full downtime suite

```powershell
pytest .
```

### Run the full suite with verbose output

```powershell
pytest . -v
```

### Run the full suite in quiet mode

```powershell
pytest . -q
```

### Stop on first failure

```powershell
pytest . -v -x
```

### Run one test file

```powershell
pytest tests/test_server_recovery.py -v
```

### Run one specific test

```powershell
pytest tests/test_server_recovery.py::test_packet_survives_graceful_server_restart_before_first_pull -v
```

### Run tests matching part of a name

```powershell
pytest . -v -k "restart"
```

## Test Files and What They Cover

## 1. `tests/test_server_recovery.py`

Covers server-side recovery and queued-message durability across restart and downtime windows.

### Run the whole file

```powershell
pytest tests/test_server_recovery.py -v
```

### `test_packet_survives_graceful_server_restart_before_first_pull`

Checks that a queued packet still exists after a normal server restart if the receiver has not pulled it yet.

Run just this test:

```powershell
pytest tests/test_server_recovery.py::test_packet_survives_graceful_server_restart_before_first_pull -v
```

### `test_packet_survives_hard_server_kill_before_first_pull`

Checks that a queued packet survives a hard kill and is still deliverable after the server comes back.

Run just this test:

```powershell
pytest tests/test_server_recovery.py::test_packet_survives_hard_server_kill_before_first_pull -v
```

### `test_packet_expires_while_server_is_down`

Checks that packet expiration is still enforced when the packet’s TTL runs out during server downtime.

Run just this test:

```powershell
pytest tests/test_server_recovery.py::test_packet_expires_while_server_is_down -v
```

## 2. `tests/test_client_recovery.py`

Covers client and worker recovery behavior after interruption and restart.

### Run the whole file

```powershell
pytest tests/test_client_recovery.py -v
```

### `test_redelivery_after_receiver_worker_crash_before_ack_and_restart`

Checks that if the receiver worker crashes before ACK, the message is redelivered after restart instead of being lost.

Run just this test:

```powershell
pytest tests/test_client_recovery.py::test_redelivery_after_receiver_worker_crash_before_ack_and_restart -v
```

### `test_long_poll_worker_can_recover_after_server_restart`

Checks that a long-polling worker can recover and continue correctly after the server restarts.

Run just this test:

```powershell
pytest tests/test_client_recovery.py::test_long_poll_worker_can_recover_after_server_restart -v
```

## 3. `tests/test_replay_and_admin_semantics.py`

Covers replay semantics and admin/destructive actions that affect queued or linked state.

### Run the whole file

```powershell
pytest tests/test_replay_and_admin_semantics.py -v
```

### `test_same_packet_id_is_accepted_again_after_final_ack`

Checks the replay rule for packet IDs after a packet has already reached final ACK state.

Run just this test:

```powershell
pytest tests/test_replay_and_admin_semantics.py::test_same_packet_id_is_accepted_again_after_final_ack -v
```

### `test_revoked_link_does_not_cancel_already_queued_packets`

Checks that revoking a link does not retroactively cancel packets that were already queued before revocation.

Run just this test:

```powershell
pytest tests/test_replay_and_admin_semantics.py::test_revoked_link_does_not_cancel_already_queued_packets -v
```

### `test_identity_delete_drops_queued_packets_on_both_sides`

Checks that deleting an identity removes queued packets associated with that identity on both sides.

Run just this test:

```powershell
pytest tests/test_replay_and_admin_semantics.py::test_identity_delete_drops_queued_packets_on_both_sides -v
```

## Recommended Basic Workflow

From repo root:

```powershell
cd tests/arqs_downtime_pytest_suite
pytest . -v
```
