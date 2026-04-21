from __future__ import annotations

import json
import subprocess
import time
import uuid
from pathlib import Path

import pytest

from .helpers import assert_http_error, link_bidirectional, raw_json_request




@pytest.mark.slow
def test_invalid_redeem_burst_stays_rejected(actor_trio):
    _, b, _ = actor_trio
    for _ in range(25):
        status, body = raw_json_request(
            "POST",
            "/links/redeem",
            json_body={"code": "ZZZZZZ", "destination_endpoint_id": b.default_endpoint_id},
            headers={"X-ARQS-API-Key": b.api_key},
        )
        assert status == 404
        assert body.get("detail") == "link code not found"


REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_DIR = REPO_ROOT / "arqs-server"


def _run_admin_cli_json(*args: str) -> dict:
    try:
        result = subprocess.run(
            ["docker", "compose", "exec", "-T", "arqs-server", "python", "-m", "app.admin_cli", *args],
            cwd=SERVER_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        pytest.skip("Docker is not available on PATH, so the live admin CLI rate-limit test cannot run.")

    if result.returncode != 0:
        pytest.skip(
            "Live admin CLI call failed, so the rate-limit test cannot manage runtime settings. "
            f"Command failed with exit code {result.returncode}. STDERR: {result.stderr.strip()!r}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AssertionError(
            "Expected JSON from dockerized admin CLI, "
            f"got STDOUT={result.stdout!r} STDERR={result.stderr!r}"
        ) from exc


def _get_live_rate_limit_settings() -> tuple[int, int]:
    body = _run_admin_cli_json("rate", "show", "--json")
    try:
        return int(body["send_window_seconds"]), int(body["max_sends_per_window"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError(f"Admin CLI rate show returned unexpected payload: {body!r}") from exc


def _set_live_rate_limit_settings(*, window_seconds: int, max_sends: int) -> None:
    body = _run_admin_cli_json(
        "rate",
        "set",
        "--send-window-seconds",
        str(window_seconds),
        "--max-sends-per-window",
        str(max_sends),
        "--json",
    )
    try:
        actual_window = int(body["send_window_seconds"])
        actual_max = int(body["max_sends_per_window"])
    except (KeyError, TypeError, ValueError) as exc:
        raise AssertionError(f"Admin CLI rate set returned unexpected payload: {body!r}") from exc
    assert actual_window == window_seconds, f"Expected send_window_seconds={window_seconds}, got {actual_window}"
    assert actual_max == max_sends, f"Expected max_sends_per_window={max_sends}, got {actual_max}"


@pytest.mark.slow
def test_send_rate_limit_enforced_and_recovers(actor_trio):
    a, b, _ = actor_trio
    link_bidirectional(a, b)

    def send_once(label: str) -> tuple[int, dict]:
        status, body = raw_json_request(
            "POST",
            "/packets",
            json_body={
                "version": 1,
                "packet_id": str(uuid.uuid4()),
                "from_endpoint_id": a.default_endpoint_id,
                "to_endpoint_id": b.default_endpoint_id,
                "headers": {},
                "body": label,
                "data": {},
                "meta": {"suite": "rate-limit"},
            },
            headers={"X-ARQS-API-Key": a.api_key},
            timeout=60.0,
        )
        return status, body

    original_window, original_max_sends = _get_live_rate_limit_settings()
    test_window_seconds = 3
    test_max_sends = 5

    try:
        _set_live_rate_limit_settings(window_seconds=test_window_seconds, max_sends=test_max_sends)

        start = time.monotonic()

        # First test_max_sends should be accepted.
        for i in range(test_max_sends):
            status, body = send_once(f"burst-{i}")
            assert status == 201, f"Expected HTTP 201 before limit, got {status} / {body!r}"

        elapsed = time.monotonic() - start
        assert elapsed < test_window_seconds, (
            f"Sequential burst took {elapsed:.2f}s which is >= test window {test_window_seconds}s. "
            "The localhost rate-limit test is no longer deterministic under current conditions."
        )

        # One more should trip the limiter.
        status, body = send_once("burst-over-limit")
        assert status == 429, f"Expected HTTP 429 after exceeding limit, got {status} / {body!r}"
        assert isinstance(body, dict)
        assert body.get("detail") == "send rate limit exceeded"

        # After the window expires, sending should recover.
        time.sleep(test_window_seconds + 0.5)

        status, body = send_once("after-window")
        assert status == 201, f"Expected HTTP 201 after window reset, got {status} / {body!r}"
    finally:
        _set_live_rate_limit_settings(
            window_seconds=original_window,
            max_sends=original_max_sends,
        )

@pytest.mark.compromise
def test_local_identity_theft_via_shared_identity_file(tmp_path, server_alive):
    from .helpers import Actor, unique_name

    a = Actor(unique_name("victim-a"), tmp_path / "victim-a").initialize()
    b = Actor(unique_name("target-b"), tmp_path / "target-b").initialize()
    thief = Actor(unique_name("thief-c"), tmp_path / "thief-c").initialize()

    shared_identity = tmp_path / "shared" / "identity.json"
    shared_identity.parent.mkdir(parents=True, exist_ok=True)
    shared_identity.write_text(a.identity_path.read_text(encoding="utf-8"), encoding="utf-8")

    compromised_client = thief.load_from_identity_path(shared_identity)
    code = compromised_client.request_link_code(a.default_endpoint_id, requested_mode="bidirectional")
    link = b.client.redeem_link_code(code.code, b.default_endpoint_id)

    assert str(link.endpoint_a_id) == a.default_endpoint_id
    assert str(link.endpoint_b_id) == b.default_endpoint_id
