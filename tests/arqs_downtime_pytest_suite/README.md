# ARQS Downtime / Recovery Pytest Suite

This suite is focused on localhost crash/restart and downtime behavior for the current ARQS server.

It is designed to exercise:
- server stop/start and hard kill recovery
- receiver crash before ACK and clean redelivery
- long-poll interruption and recovery
- packet-id replay semantics after final ACK
- TTL expiry while the server is down
- current v1 admin semantics already discussed manually

## Assumptions

- Docker Desktop / Docker CLI is installed and working on the host that runs pytest.
- The ARQS server container already exists and is named `arqs-server` by default.
- The server is reachable at `http://localhost:8080` by default.
- The suite uses real API identities and endpoints against the running localhost container.

## Environment variables

- `ARQS_BASE_URL` (default: `http://localhost:8080`)
- `ARQS_DOCKER_CONTAINER` (default: `arqs-server`)
- `ARQS_STARTUP_TIMEOUT_SECONDS` (default: `45`)
- `ARQS_WORKER_POLL_WAIT_SECONDS` (default: `10`)

## Run

From inside this folder:

```powershell
py -m pytest
```

Or explicitly:

```powershell
py -m pytest -v
```

## Notes

- These tests intentionally stop/start/kill the Docker container.
- Some tests also launch separate Python worker processes to behave like client instances, then terminate and relaunch them.
- This suite is meant for local/self-hosted recovery validation, not CI against shared infrastructure.
