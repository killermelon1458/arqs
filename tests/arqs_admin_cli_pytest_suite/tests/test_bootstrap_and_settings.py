from __future__ import annotations

import pytest


@pytest.mark.cli
def test_health_bootstraps_admin_tables_and_defaults(admin_cli):
    data = admin_cli.run_json("health", "--json")

    assert data["status"] == "ok"
    assert data["runtime_settings"]["default_ip_policy"] == "allow"
    assert data["runtime_settings"]["max_packet_bytes"] == 262144
    assert data["runtime_settings"]["max_storage_bytes"] == 1000000
    assert data["runtime_settings"]["max_inbox_batch"] == 200
    assert data["runtime_settings"]["long_poll_max_seconds"] == 30
    assert data["maintenance"]["cleanup_interval_seconds"] == 60
    assert data["maintenance"]["enabled"] is True
    assert data["ip_access"]["mode"] == "dynamic"
    assert data["ip_access"]["allow_rules_total"] == 0
    assert data["ip_access"]["deny_rules_total"] == 0

    with admin_cli.connect() as conn:
        table_names = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }

    assert "runtime_settings" in table_names
    assert "ip_access_rules" in table_names


@pytest.mark.cli
def test_limits_and_rate_commands_update_runtime_settings(admin_cli):
    initial_limits = admin_cli.run_json("limits", "show", "--json")
    assert initial_limits["max_packet_bytes"] == 262144
    assert initial_limits["max_storage_bytes"] == 1000000

    updated_limits = admin_cli.run_json(
        "limits",
        "set",
        "--max-storage-bytes",
        "2000000",
        "--max-packet-bytes",
        "1024",
        "--max-inbox-batch",
        "5",
        "--long-poll-max-seconds",
        "7",
        "--json",
    )
    assert updated_limits["max_storage_bytes"] == 2000000
    assert updated_limits["max_packet_bytes"] == 1024
    assert updated_limits["max_inbox_batch"] == 5
    assert updated_limits["long_poll_max_seconds"] == 7

    updated_rate = admin_cli.run_json(
        "rate",
        "set",
        "--send-window-seconds",
        "11",
        "--max-sends-per-window",
        "22",
        "--json",
    )
    assert updated_rate["send_window_seconds"] == 11
    assert updated_rate["max_sends_per_window"] == 22

    health = admin_cli.run_json("health", "--json")
    assert health["runtime_settings"]["max_storage_bytes"] == 2000000
    assert health["runtime_settings"]["max_packet_bytes"] == 1024
    assert health["runtime_settings"]["max_inbox_batch"] == 5
    assert health["runtime_settings"]["long_poll_max_seconds"] == 7
    assert health["runtime_settings"]["send_window_seconds"] == 11
    assert health["runtime_settings"]["max_sends_per_window"] == 22
