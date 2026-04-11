from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import typer
from sqlalchemy import func, or_, select

from .auth import generate_api_key, hash_api_key
from .db import get_config, init_db, session_scope
from .models import Delivery, DirectedRoute, Endpoint, Link, LinkCode, Node, Packet
from .services import cleanup_expired, new_uuid, utcnow

app = typer.Typer(help="ARQS admin CLI")
nodes_app = typer.Typer(help="Node commands")
endpoints_app = typer.Typer(help="Endpoint commands")
links_app = typer.Typer(help="Link commands")
link_codes_app = typer.Typer(help="Link code commands")
queue_app = typer.Typer(help="Queue commands")

app.add_typer(nodes_app, name="nodes")
app.add_typer(endpoints_app, name="endpoints")
app.add_typer(links_app, name="links")
app.add_typer(link_codes_app, name="link-codes")
app.add_typer(queue_app, name="queue")


def emit(data: Any, as_json: bool) -> None:
    if as_json:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        if isinstance(data, (dict, list)):
            typer.echo(json.dumps(data, indent=2, default=str))
        else:
            typer.echo(str(data))


def require_yes(yes: bool) -> None:
    if not yes:
        raise typer.BadParameter("destructive command requires --yes")


def row_to_dict(obj, fields: list[str]) -> dict[str, Any]:
    return {field: getattr(obj, field) for field in fields}


@app.callback()
def main() -> None:
    init_db()


@app.command()
def health(json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup = cleanup_expired(db, cfg)
        db.execute(select(1)).scalar_one()
        emit({"status": "ok", "cleanup": cleanup, "db_path": cfg.storage.db_path}, json_output)


@app.command()
def stats(json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup_expired(db, cfg)
        data = {
            "nodes_total": int(db.scalar(select(func.count()).select_from(Node)) or 0),
            "endpoints_total": int(db.scalar(select(func.count()).select_from(Endpoint)) or 0),
            "active_links_total": int(db.scalar(select(func.count()).select_from(Link).where(Link.status == "active")) or 0),
            "queued_packets_total": int(db.scalar(select(func.count()).select_from(Delivery)) or 0),
            "queued_bytes_total": int(
                db.scalar(select(func.coalesce(func.sum(Packet.payload_bytes), 0)).select_from(Delivery).join(Packet, Packet.packet_id == Delivery.packet_id))
                or 0
            ),
            "active_link_codes_total": int(db.scalar(select(func.count()).select_from(LinkCode).where(LinkCode.status == "active")) or 0),
        }
        emit(data, json_output)


@nodes_app.command("list")
def nodes_list(json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        rows = db.execute(select(Node).order_by(Node.created_at.desc())).scalars().all()
        emit([row_to_dict(r, ["node_id", "node_name", "created_at", "status"]) for r in rows], json_output)


@nodes_app.command("show")
def nodes_show(node_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise typer.Exit(code=1)
        emit(row_to_dict(node, ["node_id", "node_name", "created_at", "status"]), json_output)


@nodes_app.command("disable")
def nodes_disable(node_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise typer.Exit(code=1)
        node.status = "disabled"
        emit({"disabled": True, "node_id": node_id}, json_output)


@nodes_app.command("enable")
def nodes_enable(node_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise typer.Exit(code=1)
        node.status = "active"
        emit({"enabled": True, "node_id": node_id}, json_output)


@nodes_app.command("rotate-key")
def nodes_rotate_key(node_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise typer.Exit(code=1)
        api_key = generate_api_key()
        node.api_key_hash = hash_api_key(api_key)
        emit({"node_id": node_id, "api_key": api_key}, json_output)


@nodes_app.command("revoke")
def nodes_revoke(node_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        node = db.get(Node, node_id)
        if not node:
            raise typer.Exit(code=1)
        node.status = "revoked"
        emit({"revoked": True, "node_id": node_id}, json_output)


@endpoints_app.command("list")
def endpoints_list(json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        rows = db.execute(select(Endpoint).order_by(Endpoint.created_at.desc())).scalars().all()
        emit([row_to_dict(r, ["endpoint_id", "node_id", "endpoint_name", "kind", "created_at", "status"]) for r in rows], json_output)


@endpoints_app.command("show")
def endpoints_show(endpoint_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(Endpoint, endpoint_id)
        if not row:
            raise typer.Exit(code=1)
        emit(row_to_dict(row, ["endpoint_id", "node_id", "endpoint_name", "kind", "meta", "created_at", "status"]), json_output)


@endpoints_app.command("disable")
def endpoints_disable(endpoint_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(Endpoint, endpoint_id)
        if not row:
            raise typer.Exit(code=1)
        row.status = "disabled"
        emit({"disabled": True, "endpoint_id": endpoint_id}, json_output)


@endpoints_app.command("enable")
def endpoints_enable(endpoint_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(Endpoint, endpoint_id)
        if not row:
            raise typer.Exit(code=1)
        row.status = "active"
        emit({"enabled": True, "endpoint_id": endpoint_id}, json_output)


@links_app.command("list")
def links_list(json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        rows = db.execute(select(Link).order_by(Link.created_at.desc())).scalars().all()
        emit([row_to_dict(r, ["link_id", "endpoint_a_id", "endpoint_b_id", "mode", "created_at", "status"]) for r in rows], json_output)


@links_app.command("show")
def links_show(link_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(Link, link_id)
        if not row:
            raise typer.Exit(code=1)
        emit(row_to_dict(row, ["link_id", "endpoint_a_id", "endpoint_b_id", "mode", "created_at", "status"]), json_output)


@links_app.command("revoke")
def links_revoke(link_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(Link, link_id)
        if not row:
            raise typer.Exit(code=1)
        row.status = "revoked"
        routes = db.execute(select(DirectedRoute).where(DirectedRoute.created_by_link_id == link_id)).scalars().all()
        for route in routes:
            route.status = "revoked"
        emit({"revoked": True, "link_id": link_id, "routes_revoked": len(routes)}, json_output)


@link_codes_app.command("list")
def link_codes_list(json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup_expired(db, cfg)
        rows = db.execute(select(LinkCode).order_by(LinkCode.created_at.desc())).scalars().all()
        emit([row_to_dict(r, ["link_code_id", "code", "source_endpoint_id", "requested_mode", "created_at", "expires_at", "status"]) for r in rows], json_output)


@link_codes_app.command("show")
def link_codes_show(code_id: str, json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup_expired(db, cfg)
        row = db.get(LinkCode, code_id)
        if not row:
            raise typer.Exit(code=1)
        emit(row_to_dict(row, ["link_code_id", "code", "source_endpoint_id", "requested_mode", "created_at", "expires_at", "status"]), json_output)


@link_codes_app.command("revoke")
def link_codes_revoke(code_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        row = db.get(LinkCode, code_id)
        if not row:
            raise typer.Exit(code=1)
        row.status = "revoked"
        emit({"revoked": True, "link_code_id": code_id}, json_output)


@queue_app.command("stats")
def queue_stats(json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup_expired(db, cfg)
        data = {
            "queued_packets_total": int(db.scalar(select(func.count()).select_from(Delivery)) or 0),
            "queued_bytes_total": int(
                db.scalar(select(func.coalesce(func.sum(Packet.payload_bytes), 0)).select_from(Delivery).join(Packet, Packet.packet_id == Delivery.packet_id))
                or 0
            ),
            "oldest_queued_at": db.scalar(select(func.min(Delivery.queued_at)).select_from(Delivery)),
        }
        emit(data, json_output)


@queue_app.command("list")
def queue_list(json_output: bool = typer.Option(False, "--json")):
    cfg = get_config()
    with session_scope() as db:
        cleanup_expired(db, cfg)
        rows = db.execute(select(Delivery).order_by(Delivery.queued_at.asc())).scalars().all()
        emit([row_to_dict(r, ["delivery_id", "packet_id", "destination_node_id", "destination_endpoint_id", "queued_at", "state", "last_attempt_at"]) for r in rows], json_output)


@queue_app.command("show")
def queue_show(packet_id: str, json_output: bool = typer.Option(False, "--json")):
    with session_scope() as db:
        packet = db.get(Packet, packet_id)
        delivery = db.scalar(select(Delivery).where(Delivery.packet_id == packet_id))
        if not packet or not delivery:
            raise typer.Exit(code=1)
        emit(
            {
                "packet": row_to_dict(packet, ["packet_id", "version", "sender_node_id", "from_endpoint_id", "to_endpoint_id", "headers", "body", "data", "meta", "created_at", "expires_at", "payload_bytes"]),
                "delivery": row_to_dict(delivery, ["delivery_id", "destination_node_id", "destination_endpoint_id", "queued_at", "state", "last_attempt_at"]),
            },
            json_output,
        )


@queue_app.command("purge-node")
def queue_purge_node(node_id: str, yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    require_yes(yes)
    with session_scope() as db:
        deliveries = db.execute(select(Delivery).where(Delivery.destination_node_id == node_id)).scalars().all()
        packet_ids = [d.packet_id for d in deliveries]
        packets = db.execute(select(Packet).where(Packet.packet_id.in_(packet_ids))).scalars().all() if packet_ids else []
        for row in deliveries:
            db.delete(row)
        for row in packets:
            db.delete(row)
        emit({"purged_packets": len(packets), "node_id": node_id}, json_output)


@queue_app.command("purge-endpoint")
def queue_purge_endpoint(endpoint_id: str, yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    require_yes(yes)
    with session_scope() as db:
        deliveries = db.execute(select(Delivery).where(Delivery.destination_endpoint_id == endpoint_id)).scalars().all()
        packet_ids = [d.packet_id for d in deliveries]
        packets = db.execute(select(Packet).where(Packet.packet_id.in_(packet_ids))).scalars().all() if packet_ids else []
        for row in deliveries:
            db.delete(row)
        for row in packets:
            db.delete(row)
        emit({"purged_packets": len(packets), "endpoint_id": endpoint_id}, json_output)


@queue_app.command("purge-packet")
def queue_purge_packet(packet_id: str, yes: bool = typer.Option(False, "--yes"), json_output: bool = typer.Option(False, "--json")):
    require_yes(yes)
    with session_scope() as db:
        delivery = db.scalar(select(Delivery).where(Delivery.packet_id == packet_id))
        packet = db.get(Packet, packet_id)
        if delivery:
            db.delete(delivery)
        if packet:
            db.delete(packet)
        emit({"purged": bool(packet or delivery), "packet_id": packet_id}, json_output)


@app.command("cleanup")
def cleanup(kind: str = typer.Argument(..., help="Use: expired"), json_output: bool = typer.Option(False, "--json")):
    if kind != "expired":
        raise typer.BadParameter("only 'expired' is supported")
    cfg = get_config()
    with session_scope() as db:
        result = cleanup_expired(db, cfg)
        emit(result, json_output)


if __name__ == "__main__":
    app()
