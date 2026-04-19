from __future__ import annotations

import json
from datetime import datetime
from typing import Any

import typer

from .admin_services import (
    AdminConflictError,
    AdminNotFoundError,
    AdminValidationError,
    allow_ip,
    deny_ip,
    disable_node,
    enable_node,
    ensure_admin_tables,
    get_ip_policy,
    get_endpoint_detail,
    get_node_detail,
    get_oldest_queued_delivery_info,
    get_queue_stats_by_endpoint,
    get_queue_stats_by_node,
    get_runtime_settings,
    get_summary_stats,
    health_check,
    list_ip_rules,
    list_endpoints,
    list_link_codes_admin,
    list_links_admin,
    list_nodes,
    remove_ip_rule,
    revoke_link_admin,
    revoke_node,
    run_cleanup_now,
    set_ip_policy,
    update_runtime_settings,
)
from .db import get_config, init_db, session_scope


app = typer.Typer(help="ARQS local-only admin CLI")
stats_app = typer.Typer(help="Stats commands")
ip_app = typer.Typer(help="IP access commands")
ip_policy_app = typer.Typer(help="IP policy commands")
limits_app = typer.Typer(help="Runtime limit commands")
rate_app = typer.Typer(help="Rate-limit commands")
nodes_app = typer.Typer(help="Node commands")
endpoints_app = typer.Typer(help="Endpoint commands")
links_app = typer.Typer(help="Link commands")
link_codes_app = typer.Typer(help="Link-code commands")
cleanup_app = typer.Typer(help="Cleanup commands")

app.add_typer(stats_app, name="stats")
app.add_typer(ip_app, name="ip")
ip_app.add_typer(ip_policy_app, name="policy")
app.add_typer(limits_app, name="limits")
app.add_typer(rate_app, name="rate")
app.add_typer(nodes_app, name="nodes")
app.add_typer(endpoints_app, name="endpoints")
app.add_typer(links_app, name="links")
app.add_typer(link_codes_app, name="link-codes")
app.add_typer(cleanup_app, name="cleanup")


def _json_default(value: Any) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)



def emit(data: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=_json_default))
        return

    if isinstance(data, (dict, list)):
        typer.echo(json.dumps(data, indent=2, default=_json_default))
    else:
        typer.echo(str(data))



def fail(exc: Exception) -> None:
    if isinstance(exc, AdminValidationError):
        typer.echo(f"Validation error: {exc}", err=True)
        raise typer.Exit(code=3)
    if isinstance(exc, AdminNotFoundError):
        typer.echo(f"Not found: {exc}", err=True)
        raise typer.Exit(code=4)
    if isinstance(exc, AdminConflictError):
        typer.echo(f"Conflict: {exc}", err=True)
        raise typer.Exit(code=5)
    typer.echo(str(exc), err=True)
    raise typer.Exit(code=1)


@app.callback()
def main() -> None:
    init_db()
    with session_scope() as db:
        ensure_admin_tables(db, get_config())


@app.command()
def health(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(health_check(db), json_output)
    except Exception as exc:
        fail(exc)


@stats_app.command("summary")
def stats_summary(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(get_summary_stats(db), json_output)
    except Exception as exc:
        fail(exc)


@stats_app.command("queue-by-node")
def stats_queue_by_node(
    limit: int = typer.Option(50, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(get_queue_stats_by_node(db, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@stats_app.command("queue-by-endpoint")
def stats_queue_by_endpoint(
    limit: int = typer.Option(50, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(get_queue_stats_by_endpoint(db, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@stats_app.command("oldest-queued")
def stats_oldest_queued(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(get_oldest_queued_delivery_info(db), json_output)
    except Exception as exc:
        fail(exc)


@ip_app.command("list")
def ip_list(
    action: str | None = typer.Option(None, "--action"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(list_ip_rules(db, action=action), json_output)
    except Exception as exc:
        fail(exc)


@ip_app.command("allow")
def ip_allow(
    ip: str,
    reason: str | None = typer.Option(None, "--reason"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(allow_ip(db, ip, reason=reason), json_output)
    except Exception as exc:
        fail(exc)


@ip_app.command("deny")
@ip_app.command("block")
def ip_deny(
    ip: str,
    reason: str | None = typer.Option(None, "--reason"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(deny_ip(db, ip, reason=reason), json_output)
    except Exception as exc:
        fail(exc)


@ip_app.command("remove")
@ip_app.command("pardon")
def ip_remove(ip: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(remove_ip_rule(db, ip), json_output)
    except Exception as exc:
        fail(exc)


@ip_policy_app.command("show")
def ip_policy_show(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(get_ip_policy(db), json_output)
    except Exception as exc:
        fail(exc)


@ip_policy_app.command("set")
def ip_policy_set(
    default_ip_policy: str = typer.Option(..., "--default", help="allow or deny"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(set_ip_policy(db, default_ip_policy), json_output)
    except Exception as exc:
        fail(exc)


@limits_app.command("show")
def limits_show(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            settings = get_runtime_settings(db)
            emit(
                {
                    "max_storage_bytes": settings["max_storage_bytes"],
                    "max_packet_bytes": settings["max_packet_bytes"],
                    "max_queued_packets_per_endpoint": settings["max_queued_packets_per_endpoint"],
                    "max_queued_bytes_per_endpoint": settings["max_queued_bytes_per_endpoint"],
                    "max_queued_bytes_per_node": settings["max_queued_bytes_per_node"],
                    "max_total_queued_packets": settings["max_total_queued_packets"],
                    "max_total_queued_bytes": settings["max_total_queued_bytes"],
                    "max_inbox_batch": settings["max_inbox_batch"],
                    "long_poll_max_seconds": settings["long_poll_max_seconds"],
                    "updated_at": settings["updated_at"],
                },
                json_output,
            )
    except Exception as exc:
        fail(exc)


@limits_app.command("set")
def limits_set(
    max_storage_bytes: int | None = typer.Option(None, "--max-storage-bytes"),
    max_packet_bytes: int | None = typer.Option(None, "--max-packet-bytes"),
    max_queued_packets_per_endpoint: int | None = typer.Option(None, "--max-queued-packets-per-endpoint"),
    max_queued_bytes_per_endpoint: int | None = typer.Option(None, "--max-queued-bytes-per-endpoint"),
    max_queued_bytes_per_node: int | None = typer.Option(None, "--max-queued-bytes-per-node"),
    max_total_queued_packets: int | None = typer.Option(None, "--max-total-queued-packets"),
    max_total_queued_bytes: int | None = typer.Option(None, "--max-total-queued-bytes"),
    max_inbox_batch: int | None = typer.Option(None, "--max-inbox-batch"),
    long_poll_max_seconds: int | None = typer.Option(None, "--long-poll-max-seconds"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(
                update_runtime_settings(
                    db,
                    max_storage_bytes=max_storage_bytes,
                    max_packet_bytes=max_packet_bytes,
                    max_queued_packets_per_endpoint=max_queued_packets_per_endpoint,
                    max_queued_bytes_per_endpoint=max_queued_bytes_per_endpoint,
                    max_queued_bytes_per_node=max_queued_bytes_per_node,
                    max_total_queued_packets=max_total_queued_packets,
                    max_total_queued_bytes=max_total_queued_bytes,
                    max_inbox_batch=max_inbox_batch,
                    long_poll_max_seconds=long_poll_max_seconds,
                ),
                json_output,
            )
    except Exception as exc:
        fail(exc)


@rate_app.command("show")
def rate_show(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            settings = get_runtime_settings(db)
            emit(
                {
                    "send_window_seconds": settings["send_window_seconds"],
                    "max_sends_per_window": settings["max_sends_per_window"],
                    "updated_at": settings["updated_at"],
                },
                json_output,
            )
    except Exception as exc:
        fail(exc)


@rate_app.command("set")
def rate_set(
    send_window_seconds: int | None = typer.Option(None, "--send-window-seconds"),
    max_sends_per_window: int | None = typer.Option(None, "--max-sends-per-window"),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(
                update_runtime_settings(
                    db,
                    send_window_seconds=send_window_seconds,
                    max_sends_per_window=max_sends_per_window,
                ),
                json_output,
            )
    except Exception as exc:
        fail(exc)


@nodes_app.command("list")
def nodes_list(
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(100, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(list_nodes(db, status=status, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@nodes_app.command("show")
def nodes_show(node_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(get_node_detail(db, node_id), json_output)
    except Exception as exc:
        fail(exc)


@nodes_app.command("disable")
def nodes_disable(node_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(disable_node(db, node_id), json_output)
    except Exception as exc:
        fail(exc)


@nodes_app.command("enable")
def nodes_enable(node_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(enable_node(db, node_id), json_output)
    except Exception as exc:
        fail(exc)


@nodes_app.command("revoke")
def nodes_revoke(node_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(revoke_node(db, node_id), json_output)
    except Exception as exc:
        fail(exc)


@endpoints_app.command("list")
def endpoints_list(
    node_id: str | None = typer.Option(None, "--node-id"),
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(100, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(list_endpoints(db, node_id=node_id, status=status, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@endpoints_app.command("show")
def endpoints_show(endpoint_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(get_endpoint_detail(db, endpoint_id), json_output)
    except Exception as exc:
        fail(exc)


@links_app.command("list")
def links_list(
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(100, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(list_links_admin(db, status=status, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@links_app.command("revoke")
def links_revoke(link_id: str, json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(revoke_link_admin(db, link_id), json_output)
    except Exception as exc:
        fail(exc)


@link_codes_app.command("list")
def link_codes_list(
    status: str | None = typer.Option(None, "--status"),
    limit: int = typer.Option(100, "--limit", min=1),
    json_output: bool = typer.Option(False, "--json"),
):
    try:
        with session_scope() as db:
            emit(list_link_codes_admin(db, status=status, limit=limit), json_output)
    except Exception as exc:
        fail(exc)


@cleanup_app.command("run")
def cleanup_run(json_output: bool = typer.Option(False, "--json")):
    try:
        with session_scope() as db:
            emit(run_cleanup_now(db), json_output)
    except Exception as exc:
        fail(exc)


if __name__ == "__main__":
    app()
