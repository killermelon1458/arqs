from __future__ import annotations

import time
import uuid
from typing import Any

from fastapi import HTTPException, status

from .config import settings
from .db import cleanup_expired_state, get_conn, json_dumps, json_loads, utc_now
from .models import LinkCompleteIn, PacketIn
from .security import generate_api_key, generate_link_code, hash_api_key

CLIENT_CAPABILITIES = [
    "packet.send",
    "packet.receive",
    "packet.ack",
    "link.request",
    "self.rotate_api_key",
    "self.regenerate_client_id",
    "self.delete_identity",
    "stats.read",
]

ADAPTER_CAPABILITIES = [
    "packet.send",
    "packet.receive",
    "packet.ack",
    "link.complete",
    "adapter.delivery.receive",
    "adapter.inbound.create",
]


class RateLimitService:
    def check_and_increment_register(self, bucket_key: str) -> None:
        window = settings.register_rate_limit_window_seconds
        max_attempts = settings.register_rate_limit_max_attempts
        now = utc_now()
        window_started_at = now - (now % window)
        with get_conn() as conn:
            row = conn.execute(
                "SELECT count FROM rate_limits WHERE bucket_key = ? AND window_started_at = ?",
                (bucket_key, window_started_at),
            ).fetchone()
            if row:
                next_count = int(row["count"]) + 1
                conn.execute(
                    "UPDATE rate_limits SET count = ? WHERE bucket_key = ? AND window_started_at = ?",
                    (next_count, bucket_key, window_started_at),
                )
            else:
                next_count = 1
                conn.execute(
                    "INSERT INTO rate_limits(bucket_key, window_started_at, count) VALUES (?, ?, ?)",
                    (bucket_key, window_started_at, next_count),
                )
        if next_count > max_attempts:
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail="registration rate limit exceeded")


class ActorService:
    def register_client(self, display_name: str | None = None, client_name: str | None = None) -> dict[str, Any]:
        actor_id = str(uuid.uuid4())
        client_id = str(uuid.uuid4())
        api_key = generate_api_key()
        now = utc_now()
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO actors(actor_id, actor_type, api_key_hash, capabilities_json, adapter_type, state, display_name, created_at, revoked_at) VALUES (?, 'client', ?, ?, NULL, 'active', ?, ?, NULL)",
                (actor_id, hash_api_key(api_key), json_dumps(CLIENT_CAPABILITIES), display_name, now),
            )
            conn.execute(
                "INSERT INTO clients(client_id, owner_actor_id, client_name, created_at) VALUES (?, ?, ?, ?)",
                (client_id, actor_id, client_name, now),
            )
        return {"actor_id": actor_id, "client_id": client_id, "api_key": api_key}

    def rotate_api_key(self, actor: dict[str, Any]) -> dict[str, Any]:
        new_api_key = generate_api_key()
        with get_conn() as conn:
            conn.execute(
                "UPDATE actors SET api_key_hash = ? WHERE actor_id = ? AND actor_type = 'client' AND state = 'active'",
                (hash_api_key(new_api_key), actor["actor_id"]),
            )
        return {"actor_id": actor["actor_id"], "client_id": actor["client_id"], "api_key": new_api_key}

    def regenerate_client_id(self, actor: dict[str, Any]) -> dict[str, Any]:
        if not actor.get("client_id"):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="client actor missing client_id")
        old_client_id = actor["client_id"]
        new_client_id = str(uuid.uuid4())
        with get_conn() as conn:
            conn.execute("DELETE FROM routes WHERE client_id = ?", (old_client_id,))
            conn.execute("DELETE FROM link_codes WHERE client_id = ?", (old_client_id,))
            packet_ids = [
                row["packet_id"]
                for row in conn.execute("SELECT packet_id FROM packets WHERE client_id = ?", (old_client_id,)).fetchall()
            ]
            if packet_ids:
                conn.executemany("DELETE FROM inbox_entries WHERE packet_id = ?", [(packet_id,) for packet_id in packet_ids])
            conn.execute("DELETE FROM packets WHERE client_id = ?", (old_client_id,))
            conn.execute("UPDATE clients SET client_id = ? WHERE owner_actor_id = ?", (new_client_id, actor["actor_id"]))
        return {
            "actor_id": actor["actor_id"],
            "old_client_id": old_client_id,
            "client_id": new_client_id,
            "routes_cleared": True,
            "relink_required": True,
        }

    def delete_identity(self, actor: dict[str, Any]) -> dict[str, Any]:
        client_id = actor.get("client_id")
        with get_conn() as conn:
            conn.execute("DELETE FROM actors WHERE actor_id = ? AND actor_type = 'client'", (actor["actor_id"],))
        return {"status": "deleted", "actor_id": actor["actor_id"], "client_id": client_id}

    def issue_adapter_provision_link(self, adapter_type: str, created_by_actor_id: str | None = None) -> dict[str, Any]:
        now = utc_now()
        expires_at = now + settings.adapter_provision_ttl_seconds
        code = None
        for _ in range(10):
            candidate = generate_link_code(length=8)
            try:
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO link_codes(link_code, purpose, client_id, adapter_type, capabilities_json, created_by_actor_id, created_at, expires_at, used_at) VALUES (?, 'adapter_provision', NULL, ?, ?, ?, ?, ?, NULL)",
                        (candidate, adapter_type, json_dumps(ADAPTER_CAPABILITIES), created_by_actor_id, now, expires_at),
                    )
                code = candidate
                break
            except Exception:
                continue
        if not code:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to generate adapter provisioning code")
        return {"link_code": code, "adapter_type": adapter_type, "expires_in": settings.adapter_provision_ttl_seconds}

    def provision_adapter_from_link(self, link_code: str, adapter_type: str, display_name: str | None = None) -> dict[str, Any]:
        now = utc_now()
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT actor_id FROM actors WHERE actor_type = 'adapter' AND adapter_type = ? AND state = 'active'",
                (adapter_type,),
            ).fetchone()
            if existing:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"active adapter already exists for type {adapter_type}")

            link = conn.execute(
                "SELECT link_code, adapter_type, capabilities_json, expires_at, used_at FROM link_codes WHERE link_code = ? AND purpose = 'adapter_provision'",
                (link_code,),
            ).fetchone()
            if not link:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid adapter provisioning code")
            if link["used_at"] is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="adapter provisioning code already used")
            if link["expires_at"] < now:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="adapter provisioning code expired")
            if link["adapter_type"] != adapter_type:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="adapter provisioning code not valid for this adapter_type")

            actor_id = str(uuid.uuid4())
            api_key = generate_api_key()
            conn.execute(
                "INSERT INTO actors(actor_id, actor_type, api_key_hash, capabilities_json, adapter_type, state, display_name, created_at, revoked_at) VALUES (?, 'adapter', ?, ?, ?, 'active', ?, ?, NULL)",
                (actor_id, hash_api_key(api_key), link["capabilities_json"], adapter_type, display_name, now),
            )
            conn.execute("UPDATE link_codes SET used_at = ? WHERE link_code = ?", (now, link_code))
        return {"actor_id": actor_id, "actor_type": "adapter", "adapter_type": adapter_type, "api_key": api_key}

    def revoke_adapter(self, actor_id: str) -> dict[str, Any]:
        now = utc_now()
        with get_conn() as conn:
            actor = conn.execute(
                "SELECT actor_id, actor_type, adapter_type, state FROM actors WHERE actor_id = ?",
                (actor_id,),
            ).fetchone()
            if not actor or actor["actor_type"] != "adapter":
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="adapter actor not found")
            if actor["state"] == "revoked":
                return {"status": "revoked", "actor_id": actor["actor_id"], "adapter_type": actor["adapter_type"]}
            conn.execute(
                "UPDATE actors SET state = 'revoked', revoked_at = ? WHERE actor_id = ?",
                (now, actor_id),
            )
        return {"status": "revoked", "actor_id": actor["actor_id"], "adapter_type": actor["adapter_type"]}


class PacketService:
    def record_packet(self, packet: PacketIn, actor: dict[str, Any]) -> tuple[bool, int]:
        cleanup_expired_state()
        now = utc_now()
        with get_conn() as conn:
            existing = conn.execute("SELECT packet_id FROM packets WHERE packet_id = ?", (packet.packet_id,)).fetchone()
            if existing:
                return True, self.waiting_count_for_actor(actor["actor_id"], conn)

            client = conn.execute("SELECT client_id, owner_actor_id FROM clients WHERE client_id = ?", (packet.client_id,)).fetchone()
            if not client:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="client_id not found")

            if actor["actor_type"] == "client":
                if actor.get("client_id") != packet.client_id:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="client actors may only send for their own client_id")
            elif actor["actor_type"] == "adapter":
                if "adapter.inbound.create" not in actor["capabilities"]:
                    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="adapter missing adapter.inbound.create")
            else:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="unsupported actor_type for packet send")

            conn.execute(
                "INSERT INTO packets(packet_id, origin_actor_id, client_id, version, timestamp, headers_json, body_text, data_json, meta_json, created_at, idempotency_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    packet.packet_id,
                    actor["actor_id"],
                    packet.client_id,
                    packet.version,
                    packet.timestamp,
                    json_dumps(packet.headers),
                    packet.body,
                    json_dumps(packet.data),
                    json_dumps(packet.meta),
                    now,
                    now + settings.packet_id_ttl_seconds,
                ),
            )

            if actor["actor_type"] == "client":
                routes = conn.execute(
                    """
                    SELECT r.route_id, r.filters_json, t.target_id, t.type, t.external_id, t.config_json, t.meta_json
                    FROM routes r
                    JOIN targets t ON t.target_id = r.target_id
                    WHERE r.client_id = ?
                    """,
                    (packet.client_id,),
                ).fetchall()
                for route in routes:
                    if not self._route_matches(packet.headers, json_loads(route["filters_json"])):
                        continue
                    adapter_actor = conn.execute(
                        "SELECT actor_id FROM actors WHERE actor_type = 'adapter' AND adapter_type = ? AND state = 'active' LIMIT 1",
                        (route["type"],),
                    ).fetchone()
                    if not adapter_actor:
                        continue
                    delivery_meta = {
                        "target": {
                            "target_id": route["target_id"],
                            "type": route["type"],
                            "external_id": route["external_id"],
                            "config": json_loads(route["config_json"]),
                            "meta": json_loads(route["meta_json"]),
                        },
                        "source_kind": "client_outbound",
                        "source_actor_id": actor["actor_id"],
                    }
                    conn.execute(
                        "INSERT OR IGNORE INTO inbox_entries(actor_id, packet_id, delivery_kind, delivery_meta_json, status, created_at) VALUES (?, ?, 'adapter_delivery', ?, 'pending', ?)",
                        (adapter_actor["actor_id"], packet.packet_id, json_dumps(delivery_meta), now),
                    )
            elif actor["actor_type"] == "adapter":
                delivery_meta = {
                    "issuer": {
                        "adapter_type": actor["adapter_type"],
                        "actor_id": actor["actor_id"],
                    },
                    "source_kind": "adapter_inbound",
                }
                conn.execute(
                    "INSERT OR IGNORE INTO inbox_entries(actor_id, packet_id, delivery_kind, delivery_meta_json, status, created_at) VALUES (?, ?, 'client_inbound', ?, 'pending', ?)",
                    (client["owner_actor_id"], packet.packet_id, json_dumps(delivery_meta), now),
                )

            return False, self.waiting_count_for_actor(actor["actor_id"], conn)

    def _route_matches(self, headers: dict[str, Any], filters: dict[str, Any]) -> bool:
        if not filters:
            return True
        topics = filters.get("topics") or []
        if topics:
            topic = headers.get("topic")
            if topic not in topics:
                return False
        route_tags = set(filters.get("tags") or [])
        if route_tags:
            packet_tags = set(headers.get("tags") or [])
            if not (packet_tags & route_tags):
                return False
        return True

    def waiting_count_for_actor(self, actor_id: str, conn=None) -> int:
        query = "SELECT COUNT(*) AS count FROM inbox_entries WHERE actor_id = ? AND status != 'acked'"
        if conn is not None:
            row = conn.execute(query, (actor_id,)).fetchone()
            return int(row["count"])
        with get_conn() as local_conn:
            row = local_conn.execute(query, (actor_id,)).fetchone()
            return int(row["count"])

    def get_inbox(self, actor: dict[str, Any], wait_seconds: int) -> list[dict[str, Any]]:
        deadline = time.time() + max(wait_seconds, 0)
        while True:
            with get_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT ie.inbox_id, ie.delivery_kind, ie.delivery_meta_json, p.packet_id, p.client_id, p.timestamp,
                           p.headers_json, p.body_text, p.data_json, p.meta_json
                    FROM inbox_entries ie
                    JOIN packets p ON p.packet_id = ie.packet_id
                    WHERE ie.actor_id = ? AND ie.status != 'acked'
                    ORDER BY ie.created_at ASC
                    LIMIT 100
                    """,
                    (actor["actor_id"],),
                ).fetchall()
                if rows:
                    for row in rows:
                        conn.execute(
                            "UPDATE inbox_entries SET status = 'delivered', delivered_at = COALESCE(delivered_at, ?) WHERE inbox_id = ?",
                            (utc_now(), row["inbox_id"]),
                        )
                    return [
                        {
                            "packet_id": row["packet_id"],
                            "client_id": row["client_id"],
                            "timestamp": row["timestamp"],
                            "headers": json_loads(row["headers_json"]),
                            "body": row["body_text"] or "",
                            "data": json_loads(row["data_json"]),
                            "meta": json_loads(row["meta_json"]),
                            "delivery_kind": row["delivery_kind"],
                            "delivery": json_loads(row["delivery_meta_json"]),
                        }
                        for row in rows
                    ]
            if time.time() >= deadline:
                return []
            time.sleep(settings.inbox_poll_interval_seconds)

    def ack_packet(self, actor: dict[str, Any], packet_id: str, ack_status: str) -> int:
        with get_conn() as conn:
            entry = conn.execute(
                "SELECT inbox_id FROM inbox_entries WHERE actor_id = ? AND packet_id = ? AND status != 'acked'",
                (actor["actor_id"], packet_id),
            ).fetchone()
            if not entry:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="packet not found in actor inbox")
            conn.execute(
                "UPDATE inbox_entries SET status = 'acked', ack_status = ?, acked_at = ? WHERE inbox_id = ?",
                (ack_status, utc_now(), entry["inbox_id"]),
            )
            return self.waiting_count_for_actor(actor["actor_id"], conn)


class LinkService:
    def create_client_link_code(self, actor: dict[str, Any]) -> tuple[str, int]:
        if actor["actor_type"] != "client" or not actor.get("client_id"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="link_request is only valid for client actors")
        code = None
        now = utc_now()
        expires_at = now + settings.link_code_ttl_seconds
        for _ in range(10):
            candidate = generate_link_code()
            try:
                with get_conn() as conn:
                    conn.execute(
                        "INSERT INTO link_codes(link_code, purpose, client_id, adapter_type, capabilities_json, created_by_actor_id, created_at, expires_at, used_at) VALUES (?, 'client_link', ?, NULL, '[]', ?, ?, ?, NULL)",
                        (candidate, actor["client_id"], actor["actor_id"], now, expires_at),
                    )
                code = candidate
                break
            except Exception:
                continue
        if not code:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to generate link code")
        return code, settings.link_code_ttl_seconds

    def complete_link(self, actor: dict[str, Any], request: LinkCompleteIn) -> dict[str, Any]:
        if actor["actor_type"] != "adapter":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="link_complete is only valid for adapter actors")
        if actor.get("adapter_type") != request.adapter:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="adapter actor cannot complete link for a different adapter type")
        now = utc_now()
        with get_conn() as conn:
            link = conn.execute(
                "SELECT link_code, client_id, expires_at, used_at FROM link_codes WHERE link_code = ? AND purpose = 'client_link'",
                (request.link_code,),
            ).fetchone()
            if not link:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="invalid link code")
            if link["used_at"] is not None:
                raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="link code already used")
            if link["expires_at"] < now:
                raise HTTPException(status_code=status.HTTP_410_GONE, detail="link code expired")

            target = conn.execute(
                "SELECT target_id FROM targets WHERE type = ? AND external_id = ?",
                (request.adapter, request.external_id),
            ).fetchone()
            if target:
                target_id = target["target_id"]
                conn.execute(
                    "UPDATE targets SET config_json = ?, meta_json = ? WHERE target_id = ?",
                    (json_dumps(request.config), json_dumps(request.meta), target_id),
                )
            else:
                target_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO targets(target_id, type, external_id, config_json, meta_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (target_id, request.adapter, request.external_id, json_dumps(request.config), json_dumps(request.meta), now),
                )

            conn.execute(
                "INSERT OR IGNORE INTO routes(route_id, client_id, target_id, filters_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (str(uuid.uuid4()), link["client_id"], target_id, json_dumps(request.filters), now),
            )
            conn.execute("UPDATE link_codes SET used_at = ? WHERE link_code = ?", (now, request.link_code))

        return {"status": "ok", "client_id": link["client_id"], "target_id": target_id, "adapter": request.adapter}
