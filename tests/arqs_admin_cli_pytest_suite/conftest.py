from __future__ import annotations

import importlib.util
import shutil
import uuid
from pathlib import Path

import pytest

from tests.helpers import AdminCLIHarness


@pytest.fixture
def admin_cli() -> AdminCLIHarness:
    required_modules = ("typer", "sqlalchemy", "fastapi")
    missing = [name for name in required_modules if importlib.util.find_spec(name) is None]
    if missing:
        pytest.skip(
            "admin CLI tests require the arqs-server Python dependencies; "
            f"missing modules: {', '.join(missing)}. Install arqs-server/requirements.txt first"
        )

    repo_root = Path(__file__).resolve().parents[2]
    server_dir = repo_root / "arqs-server"
    temp_root = server_dir / "data" / ".pytest_admin_cli_tmp"
    temp_root.mkdir(parents=True, exist_ok=True)
    case_dir = temp_root / f"admin-cli-{uuid.uuid4().hex}"
    case_dir.mkdir(parents=True, exist_ok=True)
    db_path = case_dir / "arqs_admin_cli_suite.db"
    config_path = case_dir / "config.toml"

    config_path.write_text(
        "\n".join(
            [
                "[server]",
                'host = "127.0.0.1"',
                "port = 8080",
                'app_name = "ARQS Admin CLI Test"',
                'api_key_header = "X-ARQS-API-Key"',
                "",
                "[storage]",
                f'db_path = "{db_path.as_posix()}"',
                "sqlite_wal = false",
                "sqlite_busy_timeout_ms = 5000",
                "",
                "[retention]",
                "default_packet_ttl_seconds = 2592000",
                "no_expiry = false",
                "link_code_ttl_seconds = 600",
                "",
                "[limits]",
                "max_storage_bytes = 1000000",
                "max_packet_bytes = 262144",
                "max_queued_packets_per_endpoint = 10000",
                "max_queued_bytes_per_endpoint = 1073741824",
                "max_queued_bytes_per_node = 5368709120",
                "max_total_queued_packets = 100000",
                "max_total_queued_bytes = 42949672960",
                "max_inbox_batch = 200",
                "long_poll_max_seconds = 30",
                "",
                "[rate_limit]",
                "send_window_seconds = 60",
                "max_sends_per_window = 600",
                "",
                "[network]",
                "trusted_proxies = []",
                'trusted_forwarded_headers = ["x-forwarded-for", "x-real-ip"]',
                'ip_access_mode = "dynamic"',
                "",
                "[maintenance]",
                "cleanup_interval_seconds = 60",
                "",
                "[blacklist]",
                "client_ips = []",
                "node_ids = []",
                "",
            ]
        ),
        encoding="utf-8",
    )

    try:
        yield AdminCLIHarness(server_dir=server_dir, config_path=config_path, db_path=db_path)
    finally:
        shutil.rmtree(case_dir, ignore_errors=True)
