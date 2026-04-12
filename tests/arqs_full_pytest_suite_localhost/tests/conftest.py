from __future__ import annotations

import pytest

from .helpers import BASE_URL, create_trio, raw_json_request


def pytest_addoption(parser):
    parser.addoption("--run-slow", action="store_true", default=False, help="run slower/noisier localhost-safe tests")
    parser.addoption(
        "--run-compromise",
        action="store_true",
        default=False,
        help="run tests that intentionally simulate local identity theft/shared storage",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: localhost-safe but noisy or heavier tests")
    config.addinivalue_line("markers", "compromise: tests that intentionally simulate local identity theft/shared storage")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-slow"):
        skip_slow = pytest.mark.skip(reason="need --run-slow option to run")
        for item in items:
            if "slow" in item.keywords:
                item.add_marker(skip_slow)

    if not config.getoption("--run-compromise"):
        skip_compromise = pytest.mark.skip(reason="need --run-compromise option to run")
        for item in items:
            if "compromise" in item.keywords:
                item.add_marker(skip_compromise)


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def server_alive(base_url: str):
    status, body = raw_json_request("GET", "/health")
    assert status == 200, f"Server at {base_url} did not answer /health with 200; got {status} / {body!r}"
    return body


@pytest.fixture
def actor_trio(tmp_path, server_alive):
    return create_trio(tmp_path)
