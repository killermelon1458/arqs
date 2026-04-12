# ARQS localhost pytest suite

This suite is a black-box validation harness for a running ARQS server.

Default target:
- `http://localhost:8080`

## What it covers

- public endpoint exposure (`/health`, `/stats`, `/register`)
- auth failures (missing key, bad key, bearer auth, rotate-key invalidation)
- endpoint ownership and deletion rules
- link-code lifecycle and duplicate prevention
- route enforcement and one-way vs bidirectional behavior
- packet duplicate/idempotency rules
- inbox isolation and ACK ownership
- response-code and `detail` validation where stable
- concurrency races (redeem race, duplicate packet race)
- optional localhost abuse/slow tests (invalid redeem bursts, send rate limit, oversize payload, local identity theft / shared storage)

## Install

```bash
pip install pytest
```

The suite vendors `arqs_api.py`, so no extra package install is required.

## Run

Fast/default tests:

```bash
pytest -q
```

Include slow / abusive localhost-safe tests:

```bash
pytest -q --run-slow
```

Include compromise / shared-identity tests:

```bash
pytest -q --run-compromise
```

Run everything:

```bash
pytest -q --run-slow --run-compromise
```

Override server URL:

```bash
ARQS_BASE_URL=http://localhost:8080 pytest -q
```

## Notes

- Tests create fresh nodes/endpoints and do **not** assume an empty database.
- Tests are written to be black-box against a live server.
- Some global-cap tests (storage cap, total queue cap) are intentionally not included because they are impractical to hit in a routine localhost suite without changing server config.
- Rate-limit tests are included but marked `slow` because they intentionally generate heavy request bursts.
