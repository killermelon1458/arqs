from __future__ import annotations

import pytest


@pytest.mark.cli
@pytest.mark.ip
def test_ip_policy_guard_and_rule_lifecycle(admin_cli):
    policy = admin_cli.run_json("ip", "policy", "show", "--json")
    assert policy["default_ip_policy"] == "allow"

    denied_without_allow = admin_cli.run(
        "ip",
        "policy",
        "set",
        "--default",
        "deny",
        "--json",
        expect=5,
    )
    assert "cannot set default_ip_policy=deny" in denied_without_allow.stderr

    allow_result = admin_cli.run_json(
        "ip",
        "allow",
        "127.0.0.1",
        "--reason",
        "localhost admin",
        "--json",
    )
    assert allow_result["allowed"] is True
    assert allow_result["action"] == "allow"
    assert allow_result["ip"] == "127.0.0.1"

    deny_result = admin_cli.run_json(
        "ip",
        "block",
        "203.0.113.10",
        "--reason",
        "test deny alias",
        "--json",
    )
    assert deny_result["denied"] is True
    assert deny_result["action"] == "deny"
    assert deny_result["ip"] == "203.0.113.10"

    deny_list = admin_cli.run_json("ip", "list", "--action", "deny", "--json")
    assert len(deny_list) == 1
    assert deny_list[0]["ip"] == "203.0.113.10"
    assert deny_list[0]["action"] == "deny"

    allow_list = admin_cli.run_json("ip", "list", "--action", "allow", "--json")
    assert len(allow_list) == 1
    assert allow_list[0]["ip"] == "127.0.0.1"
    assert allow_list[0]["action"] == "allow"

    deny_policy = admin_cli.run_json("ip", "policy", "set", "--default", "deny", "--json")
    assert deny_policy["default_ip_policy"] == "deny"

    remove_result = admin_cli.run_json("ip", "pardon", "203.0.113.10", "--json")
    assert remove_result["removed"] is True
    assert remove_result["ip"] == "203.0.113.10"

    all_rules = admin_cli.run_json("ip", "list", "--json")
    assert len(all_rules) == 1
    assert all_rules[0]["ip"] == "127.0.0.1"


@pytest.mark.cli
@pytest.mark.ip
def test_ip_commands_validate_bad_input(admin_cli):
    invalid_ip = admin_cli.run("ip", "allow", "not-an-ip", "--json", expect=3)
    assert "Validation error:" in invalid_ip.stderr
    assert "invalid IP address" in invalid_ip.stderr

    invalid_action = admin_cli.run("ip", "list", "--action", "bogus", "--json", expect=3)
    assert "Validation error:" in invalid_action.stderr
    assert "action must be one of" in invalid_action.stderr
