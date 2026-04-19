# ARQS Admin CLI Pytest Suite

This suite validates the local-only admin CLI against an isolated temporary ARQS database.

It is designed to exercise:
- admin CLI bootstrap and health output
- runtime settings updates for limits and rate controls
- IP access policy commands and guardrails
- node, endpoint, link, and link-code admin commands against seeded state
- cleanup behavior for expired records
- compatibility of the legacy `app.cli` shim entrypoint

## Assumptions

- Python is installed locally.
- `pytest` is installed in the Python environment used to run the suite.
- The ARQS server repo exists in this workspace under `arqs-server/`.

This suite does **not** require:
- a running ARQS HTTP server
- Docker
- network access

## Install

```powershell
py -m pip install pytest
py -m pip install -r ..\..\arqs-server\requirements.txt
```

## Run

From inside this folder:

```powershell
py -m pytest -q
```

Or explicitly from the repo root:

```powershell
py -m pytest -q .\tests\arqs_admin_cli_pytest_suite
```

## Notes

- Each test uses its own temporary config file and SQLite database.
- The suite invokes the CLI through `python -m app.admin_cli`, so it exercises the real Typer command surface.
- Some tests seed database rows directly so the CLI can be validated without needing the public HTTP API.
