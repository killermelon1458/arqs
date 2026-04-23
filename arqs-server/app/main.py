from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .admin_services import ensure_admin_tables, get_runtime_settings
from .auth import (
    AuthenticatedNodeContext,
    generate_api_key,
    get_client_ip,
    hash_api_key,
    require_node,
    require_node_context,
)
from .db import SessionLocal, get_config, get_db, init_db, session_scope
from .inbox_notifier import InboxNotifier
from .models import Delivery, DirectedRoute, Endpoint, Link, LinkCode, Node, Packet, SendEvent
from .runtime_access_cache import (
    get_inbox_limits_cached,
    is_ip_allowed_cached,
    start_runtime_access_cache_watcher,
    stop_runtime_access_cache_watcher,
)
from .schemas import (
    DeleteIdentityResponse,
    EndpointCreateRequest,
    EndpointOut,
    HealthResponse,
    InboxResponse,
    LinkCodeRequest,
    LinkCodeResponse,
    LinkOut,
    LinkRedeemRequest,
    PacketAckRequest,
    PacketAckResponse,
    PacketSendRequest,
    PacketSendResponse,
    RegisterRequest,
    RegisterResponse,
    RotateKeyResponse,
    StatsResponse,
)
from .services import (
    active_route_exists,
    active_link_code_clause,
    active_packet_clause,
    cleanup_expired,
    enforce_queue_limits,
    enforce_send_rate_limit,
    ensure_node_active,
    ensure_node_owns_endpoint,
    is_packet_expired,
    exact_active_link_exists,
    generate_link_code,
    new_uuid,
    packet_expiry,
    packet_matches,
    payload_size_bytes,
    resolve_redeem_routes,
    utcnow,
)

cfg = get_config()
app = FastAPI(title=cfg.server.app_name, version="1.0.0")
init_db()


def _build_app_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            fmt="%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%SZ",
        )
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


logger = _build_app_logger("arqs.app")
request_logger = _build_app_logger("arqs.request")
_maintenance_stop_event = threading.Event()
_maintenance_thread: threading.Thread | None = None
inbox_notifier = InboxNotifier()


def _request_log_level(status_code: int) -> int:
    if status_code >= 500:
        return logging.ERROR
    if status_code >= 400:
        return logging.WARNING
    return logging.INFO


def _log_request_event(
    *,
    message: str,
    method: str,
    path: str,
    query: str,
    status_code: int,
    duration_ms: float,
    direct_peer_ip: str,
    direct_peer_port: int | None,
    effective_client_ip: str,
    node_id: str | None = None,
    detail: str | None = None,
    exc_info: bool = False,
) -> None:
    level = _request_log_level(status_code)
    parts = [
        message,
        f"method={method}",
        f"path={path}",
        f"query={query or '-'}",
        f"status={status_code}",
        f"duration_ms={duration_ms:.2f}",
        f"direct_peer_ip={direct_peer_ip}",
        f"direct_peer_port={direct_peer_port if direct_peer_port is not None else '-'}",
        f"effective_client_ip={effective_client_ip}",
        f"node_id={node_id or '-'}",
    ]
    if detail:
        parts.append(f"detail={detail}")
    request_logger.log(level, " ".join(parts), exc_info=exc_info)


def _maintenance_loop(stop_event: threading.Event) -> None:
    interval = int(cfg.maintenance.cleanup_interval_seconds)
    if interval <= 0:
        return
    while not stop_event.wait(interval):
        try:
            with session_scope() as db:
                cleanup_expired(db, cfg)
        except Exception:
            logger.exception("ARQS maintenance cleanup failed")


def _load_inbox_limits() -> tuple[int, int]:
    return get_inbox_limits_cached(cfg)


def _fetch_inbox_deliveries(node_id: str, limit: int) -> list[dict]:
    with SessionLocal() as db:
        rows = db.execute(
            select(Delivery, Packet)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(
                Delivery.destination_node_id == node_id,
                Delivery.state == "queued",
                active_packet_clause(now=utcnow()),
            )
            .order_by(Delivery.queued_at.asc())
            .limit(limit)
        ).all()

    results = []
    for delivery, packet in rows:
        results.append(
            {
                "delivery_id": delivery.delivery_id,
                "destination_endpoint_id": delivery.destination_endpoint_id,
                "queued_at": delivery.queued_at,
                "state": delivery.state,
                "last_attempt_at": delivery.last_attempt_at,
                "packet": {
                    "packet_id": packet.packet_id,
                    "version": packet.version,
                    "from_endpoint_id": packet.from_endpoint_id,
                    "to_endpoint_id": packet.to_endpoint_id,
                    "headers": packet.headers,
                    "body": packet.body,
                    "data": packet.data,
                    "meta": packet.meta,
                    "created_at": packet.created_at,
                    "expires_at": packet.expires_at,
                },
            }
        )
    return results


def _enforce_observability_mode(request: Request, db: Session, mode: str) -> None:
    if mode == "off":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if mode == "public":
        return
    if mode == "node_api_key":
        require_node(
            request=request,
            db=db,
            x_arqs_api_key=request.headers.get(cfg.server.api_key_header),
            authorization=request.headers.get("Authorization"),
        )
        return
    raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="invalid observability mode")


@app.on_event("startup")
def on_startup() -> None:
    global _maintenance_thread
    init_db()
    with session_scope() as db:
        ensure_admin_tables(db, cfg)
    start_runtime_access_cache_watcher(cfg)
    _maintenance_stop_event.clear()
    interval = int(cfg.maintenance.cleanup_interval_seconds)
    if interval > 0 and (_maintenance_thread is None or not _maintenance_thread.is_alive()):
        _maintenance_thread = threading.Thread(
            target=_maintenance_loop,
            args=(_maintenance_stop_event,),
            name="arqs-maintenance",
            daemon=True,
        )
        _maintenance_thread.start()


@app.on_event("shutdown")
def on_shutdown() -> None:
    global _maintenance_thread
    _maintenance_stop_event.set()
    if _maintenance_thread is not None and _maintenance_thread.is_alive():
        _maintenance_thread.join(timeout=2.0)
    _maintenance_thread = None
    stop_runtime_access_cache_watcher()


@app.middleware("http")
async def enforce_ip_policy_and_no_store(request: Request, call_next):
    started = time.perf_counter()
    direct_peer_ip = request.client.host if request.client else "unknown"
    direct_peer_port = request.client.port if request.client else None
    client_ip = get_client_ip(request)
    method = request.method
    path = request.url.path
    query = request.url.query
    if not is_ip_allowed_cached(client_ip, cfg=cfg):
        response = JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={"detail": "client IP denied"},
        )
        response.headers["Cache-Control"] = "no-store"
        _log_request_event(
            message="request_denied",
            method=method,
            path=path,
            query=query,
            status_code=response.status_code,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            direct_peer_ip=direct_peer_ip,
            direct_peer_port=direct_peer_port,
            effective_client_ip=client_ip,
            detail="client IP denied",
        )
        return response

    try:
        response: Response = await call_next(request)
    except Exception as exc:
        _log_request_event(
            message="request_exception",
            method=method,
            path=path,
            query=query,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            duration_ms=(time.perf_counter() - started) * 1000.0,
            direct_peer_ip=direct_peer_ip,
            direct_peer_port=direct_peer_port,
            effective_client_ip=client_ip,
            node_id=getattr(request.state, "node_id", None),
            detail=type(exc).__name__,
            exc_info=True,
        )
        raise

    response.headers["Cache-Control"] = "no-store"
    _log_request_event(
        message="request_complete",
        method=method,
        path=path,
        query=query,
        status_code=int(response.status_code),
        duration_ms=(time.perf_counter() - started) * 1000.0,
        direct_peer_ip=direct_peer_ip,
        direct_peer_port=direct_peer_port,
        effective_client_ip=client_ip,
        node_id=getattr(request.state, "node_id", None),
    )
    return response


@app.post("/register", response_model=RegisterResponse)
def register(payload: RegisterRequest, db: Annotated[Session, Depends(get_db)]):
    now = utcnow()
    node_id = new_uuid()
    endpoint_id = new_uuid()
    key_id, api_key = generate_api_key()

    node = Node(
        node_id=node_id,
        key_id=key_id,
        api_key_hash=hash_api_key(api_key),
        node_name=payload.node_name,
        created_at=now,
        status="active",
    )
    endpoint = Endpoint(
        endpoint_id=endpoint_id,
        node_id=node_id,
        endpoint_name="default",
        kind="default",
        meta={},
        created_at=now,
        status="active",
    )
    db.add(node)
    db.flush()
    db.add(endpoint)
    db.commit()
    return RegisterResponse(node_id=node_id, api_key=api_key, default_endpoint_id=endpoint_id)


@app.post("/identity/rotate-key", response_model=RotateKeyResponse)
def self_rotate_key(node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    ensure_node_active(node)
    _key_id, api_key = generate_api_key(node.key_id)
    node.api_key_hash = hash_api_key(api_key)
    db.add(node)
    db.commit()
    return RotateKeyResponse(node_id=node.node_id, api_key=api_key)

@app.delete("/identity", response_model=DeleteIdentityResponse)
def delete_identity(node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    endpoint_ids = [
        row[0]
        for row in db.execute(
            select(Endpoint.endpoint_id).where(Endpoint.node_id == node.node_id)
        ).all()
    ]

    endpoints_deleted = len(endpoint_ids)

    if endpoint_ids:
        links_deleted = int(
            db.scalar(
                select(func.count()).select_from(Link).where(
                    or_(
                        Link.endpoint_a_id.in_(endpoint_ids),
                        Link.endpoint_b_id.in_(endpoint_ids),
                    )
                )
            )
            or 0
        )

        routes_deleted = int(
            db.scalar(
                select(func.count()).select_from(DirectedRoute).where(
                    or_(
                        DirectedRoute.from_endpoint_id.in_(endpoint_ids),
                        DirectedRoute.to_endpoint_id.in_(endpoint_ids),
                    )
                )
            )
            or 0
        )

        link_codes_deleted = int(
            db.scalar(
                select(func.count()).select_from(LinkCode).where(
                    LinkCode.source_endpoint_id.in_(endpoint_ids)
                )
            )
            or 0
        )

        packets_deleted = int(
            db.scalar(
                select(func.count()).select_from(Packet).where(
                    or_(
                        Packet.sender_node_id == node.node_id,
                        Packet.from_endpoint_id.in_(endpoint_ids),
                        Packet.to_endpoint_id.in_(endpoint_ids),
                    )
                )
            )
            or 0
        )

        deliveries_deleted = int(
            db.scalar(
                select(func.count()).select_from(Delivery).where(
                    or_(
                        Delivery.destination_node_id == node.node_id,
                        Delivery.destination_endpoint_id.in_(endpoint_ids),
                    )
                )
            )
            or 0
        )
    else:
        links_deleted = 0
        routes_deleted = 0
        link_codes_deleted = 0
        packets_deleted = int(
            db.scalar(
                select(func.count()).select_from(Packet).where(
                    Packet.sender_node_id == node.node_id
                )
            )
            or 0
        )
        deliveries_deleted = int(
            db.scalar(
                select(func.count()).select_from(Delivery).where(
                    Delivery.destination_node_id == node.node_id
                )
            )
            or 0
        )

    send_events_deleted = int(
        db.scalar(
            select(func.count()).select_from(SendEvent).where(
                SendEvent.node_id == node.node_id
            )
        )
        or 0
    )

    node_id = node.node_id
    db.delete(node)
    db.commit()

    return DeleteIdentityResponse(
        deleted=True,
        node_id=node_id,
        endpoints_deleted=endpoints_deleted,
        links_deleted=links_deleted,
        routes_deleted=routes_deleted,
        link_codes_deleted=link_codes_deleted,
        packets_deleted=packets_deleted,
        deliveries_deleted=deliveries_deleted,
        send_events_deleted=send_events_deleted,
    )

@app.get("/endpoints", response_model=list[EndpointOut])
def list_endpoints(node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    rows = db.execute(
        select(Endpoint).where(Endpoint.node_id == node.node_id).order_by(Endpoint.created_at.asc())
    ).scalars().all()
    return rows


@app.post("/endpoints", response_model=EndpointOut, status_code=status.HTTP_201_CREATED)
def create_endpoint(
    payload: EndpointCreateRequest,
    node: Annotated[Node, Depends(require_node)],
    db: Annotated[Session, Depends(get_db)],
):
    ensure_node_active(node)
    endpoint = Endpoint(
        endpoint_id=new_uuid(),
        node_id=node.node_id,
        endpoint_name=payload.endpoint_name,
        kind=payload.kind,
        meta=payload.meta or {},
        created_at=utcnow(),
        status="active",
    )
    db.add(endpoint)
    db.commit()
    db.refresh(endpoint)
    return endpoint


@app.delete("/endpoints/{endpoint_id}")
def delete_endpoint(endpoint_id: str, node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    now = utcnow()
    endpoint = ensure_node_owns_endpoint(db, node.node_id, endpoint_id)
    if endpoint.endpoint_name == "default":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="default endpoint deletion not allowed")
    linked = db.scalar(
        select(func.count()).select_from(Link).where(
            Link.status == "active",
            or_(Link.endpoint_a_id == endpoint_id, Link.endpoint_b_id == endpoint_id),
        )
    )
    queued = db.scalar(
        select(func.count(Delivery.delivery_id))
        .select_from(Delivery)
        .join(Packet, Packet.packet_id == Delivery.packet_id)
        .where(Delivery.destination_endpoint_id == endpoint_id, active_packet_clause(now=now))
    )
    if linked or queued:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="endpoint still linked or queued")
    db.delete(endpoint)
    db.commit()
    return {"deleted": True, "endpoint_id": endpoint_id}


@app.post("/links/request", response_model=LinkCodeResponse, status_code=status.HTTP_201_CREATED)
def request_link_code(
    payload: LinkCodeRequest,
    node: Annotated[Node, Depends(require_node)],
    db: Annotated[Session, Depends(get_db)],
):
    ensure_node_active(node)
    ensure_node_owns_endpoint(db, node.node_id, str(payload.source_endpoint_id))
    now = utcnow()

    code_value = None
    for _ in range(20):
        candidate = generate_link_code()
        exists = db.scalar(select(LinkCode.link_code_id).where(LinkCode.code == candidate))
        if not exists:
            code_value = candidate
            break
    if code_value is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="failed to allocate link code")

    record = LinkCode(
        link_code_id=new_uuid(),
        code=code_value,
        source_endpoint_id=str(payload.source_endpoint_id),
        requested_mode=payload.requested_mode,
        created_at=now,
        expires_at=now + timedelta(seconds=cfg.retention.link_code_ttl_seconds),
        status="active",
    )
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


@app.post("/links/redeem", response_model=LinkOut, status_code=status.HTTP_201_CREATED)
def redeem_link_code(
    payload: LinkRedeemRequest,
    node: Annotated[Node, Depends(require_node)],
    db: Annotated[Session, Depends(get_db)],
):
    ensure_node_active(node)
    dest_endpoint = ensure_node_owns_endpoint(db, node.node_id, str(payload.destination_endpoint_id))

    code = db.scalar(select(LinkCode).where(LinkCode.code == payload.code))
    if code is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link code not found")

    if code.expires_at <= utcnow():
        code.status = "expired"
        db.add(code)
        db.commit()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="link code expired")

    if code.status != "active":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="link code not active")

    if code.source_endpoint_id == str(dest_endpoint.endpoint_id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="cannot self-link endpoint")

    # Atomic claim step:
    # exactly one concurrent redeemer should be able to transition this code
    # from active -> used. The loser gets a clean 409.
    claim = db.execute(
        update(LinkCode)
        .where(
            LinkCode.link_code_id == code.link_code_id,
            LinkCode.status == "active",
        )
        .values(status="used")
    )
    if claim.rowcount != 1:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="link code not active")

    source_endpoint = db.get(Endpoint, code.source_endpoint_id)
    if source_endpoint is None or source_endpoint.status != "active":
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="source endpoint unavailable")

    if exact_active_link_exists(db, code.source_endpoint_id, str(dest_endpoint.endpoint_id), code.requested_mode):
        db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="duplicate active link")

    routes = resolve_redeem_routes(code.source_endpoint_id, str(dest_endpoint.endpoint_id), code.requested_mode)
    for route_from, route_to in routes:
        if active_route_exists(db, route_from, route_to):
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="active route already exists")

    now = utcnow()
    link = Link(
        link_id=new_uuid(),
        endpoint_a_id=code.source_endpoint_id,
        endpoint_b_id=str(dest_endpoint.endpoint_id),
        mode=code.requested_mode,
        created_at=now,
        status="active",
    )
    db.add(link)
    db.flush()

    for route_from, route_to in routes:
        db.add(
            DirectedRoute(
                route_id=new_uuid(),
                from_endpoint_id=route_from,
                to_endpoint_id=route_to,
                created_at=now,
                status="active",
                created_by_link_id=link.link_id,
            )
        )

    db.commit()
    db.refresh(link)
    return link


@app.get("/links", response_model=list[LinkOut])
def list_links(node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    endpoint_ids = [
        row[0]
        for row in db.execute(
            select(Endpoint.endpoint_id).where(Endpoint.node_id == node.node_id)
        ).all()
    ]
    if not endpoint_ids:
        return []

    rows = db.execute(
        select(Link)
        .where(
            Link.status == "active",
            or_(Link.endpoint_a_id.in_(endpoint_ids), Link.endpoint_b_id.in_(endpoint_ids)),
        )
        .order_by(Link.created_at.desc())
    ).scalars().all()
    return rows

@app.delete("/links/{link_id}")
def revoke_link(link_id: str, node: Annotated[Node, Depends(require_node)], db: Annotated[Session, Depends(get_db)]):
    link = db.get(Link, link_id)
    if link is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="link not found")
    endpoint_ids = {row[0] for row in db.execute(select(Endpoint.endpoint_id).where(Endpoint.node_id == node.node_id)).all()}
    if link.endpoint_a_id not in endpoint_ids and link.endpoint_b_id not in endpoint_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="link not visible to node")
    link.status = "revoked"
    routes = db.execute(select(DirectedRoute).where(DirectedRoute.created_by_link_id == link.link_id)).scalars().all()
    for route in routes:
        route.status = "revoked"
        db.add(route)
    db.add(link)
    db.commit()
    return {"revoked": True, "link_id": link_id}


@app.post("/packets", response_model=PacketSendResponse, status_code=status.HTTP_201_CREATED)
def send_packet(
    payload: PacketSendRequest,
    node: Annotated[Node, Depends(require_node)],
    db: Annotated[Session, Depends(get_db)],
):
    ensure_node_active(node)
    ensure_node_owns_endpoint(db, node.node_id, str(payload.from_endpoint_id))
    dest_endpoint = db.get(Endpoint, str(payload.to_endpoint_id))
    if dest_endpoint is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="destination endpoint not found")
    if dest_endpoint.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="destination endpoint not active")
    dest_node = db.get(Node, dest_endpoint.node_id)
    if dest_node is None or dest_node.status != "active":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="destination node not active")
    if not active_route_exists(db, str(payload.from_endpoint_id), str(payload.to_endpoint_id)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="no active directed route")

    packet_bytes = payload_size_bytes(headers=payload.headers, body=payload.body, data=payload.data, meta=payload.meta)
    runtime_settings = get_runtime_settings(db)
    if packet_bytes > int(runtime_settings["max_packet_bytes"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="packet exceeds max packet bytes")

    existing = db.get(Packet, str(payload.packet_id))
    if existing is not None and is_packet_expired(existing):
        db.delete(existing)
        db.flush()
        existing = None
    if existing is not None:
        if packet_matches(
            existing,
            sender_node_id=node.node_id,
            from_endpoint_id=str(payload.from_endpoint_id),
            to_endpoint_id=str(payload.to_endpoint_id),
            headers=payload.headers,
            body=payload.body,
            data=payload.data,
            meta=payload.meta,
            version=payload.version,
        ):
            delivery = db.scalar(select(Delivery).where(Delivery.packet_id == existing.packet_id))
            return PacketSendResponse(result="duplicate", packet_id=existing.packet_id, delivery_id=(delivery.delivery_id if delivery else None), expires_at=existing.expires_at)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="packet_id already used for different packet")

    enforce_send_rate_limit(db, cfg, node.node_id)
    enforce_queue_limits(db, cfg, str(payload.to_endpoint_id), dest_node.node_id, packet_bytes)

    now = utcnow()
    packet = Packet(
        packet_id=str(payload.packet_id),
        version=payload.version,
        sender_node_id=node.node_id,
        from_endpoint_id=str(payload.from_endpoint_id),
        to_endpoint_id=str(payload.to_endpoint_id),
        headers=payload.headers,
        body=payload.body,
        data=payload.data,
        meta=payload.meta,
        created_at=now,
        expires_at=packet_expiry(now, cfg, payload.ttl_seconds),
        payload_bytes=packet_bytes,
    )
    delivery = Delivery(
        delivery_id=new_uuid(),
        packet_id=packet.packet_id,
        destination_node_id=dest_node.node_id,
        destination_endpoint_id=str(payload.to_endpoint_id),
        queued_at=now,
        state="queued",
        last_attempt_at=None,
    )

    try:
        db.add(packet)
        db.flush()
        db.add(delivery)
        db.commit()
        inbox_notifier.notify(dest_node.node_id)
        return PacketSendResponse(
            result="accepted",
            packet_id=packet.packet_id,
            delivery_id=delivery.delivery_id,
            expires_at=packet.expires_at,
        )
    except IntegrityError:
        db.rollback()

        existing = None
        existing_delivery = None

        # Give the competing request a moment to finish committing so we can
        # translate the race cleanly instead of leaking a 500.
        for _ in range(5):
            existing = db.get(Packet, str(payload.packet_id))
            if existing is not None and is_packet_expired(existing):
                db.delete(existing)
                db.flush()
                existing = None
            if existing is not None:
                existing_delivery = db.scalar(select(Delivery).where(Delivery.packet_id == existing.packet_id))
                break
            time.sleep(0.05)

        if existing is not None:
            if packet_matches(
                existing,
                sender_node_id=node.node_id,
                from_endpoint_id=str(payload.from_endpoint_id),
                to_endpoint_id=str(payload.to_endpoint_id),
                headers=payload.headers,
                body=payload.body,
                data=payload.data,
                meta=payload.meta,
                version=payload.version,
            ):
                return PacketSendResponse(
                    result="duplicate",
                    packet_id=existing.packet_id,
                    delivery_id=(existing_delivery.delivery_id if existing_delivery else None),
                    expires_at=existing.expires_at,
                )

            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="packet_id already used for different packet",
            )

        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="packet_id collision during concurrent send",
        )

@app.get("/inbox", response_model=InboxResponse)
async def poll_inbox(
    node: Annotated[AuthenticatedNodeContext, Depends(require_node_context)],
    wait: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1),
):
    ensure_node_active(node)
    max_wait, max_limit = _load_inbox_limits()
    wait = min(wait, max_wait)
    limit = min(limit, max_limit)

    deadline = time.monotonic() + wait
    while True:
        inbox_version = inbox_notifier.snapshot(node.node_id)
        deliveries = await run_in_threadpool(_fetch_inbox_deliveries, node.node_id, limit)
        if deliveries or time.monotonic() >= deadline:
            return InboxResponse(deliveries=deliveries)

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue

        await inbox_notifier.wait_for_change(
            node.node_id,
            after_version=inbox_version,
            timeout=remaining,
        )


@app.post("/packet_ack", response_model=PacketAckResponse)
def ack_packet(
    payload: PacketAckRequest,
    node: Annotated[Node, Depends(require_node)],
    db: Annotated[Session, Depends(get_db)],
):
    ensure_node_active(node)

    delivery_row = None
    now = utcnow()
    if payload.delivery_id:
        delivery_row = db.execute(
            select(Delivery, Packet)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(Delivery.delivery_id == str(payload.delivery_id), active_packet_clause(now=now))
        ).first()
    elif payload.packet_id:
        delivery_row = db.execute(
            select(Delivery, Packet)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(Packet.packet_id == str(payload.packet_id), active_packet_clause(now=now))
        ).first()
    if delivery_row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="delivery not found")
    delivery, packet = delivery_row
    if delivery.destination_node_id != node.node_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="delivery not owned by node")

    packet_id = delivery.packet_id
    db.delete(delivery)
    if packet is not None:
        db.delete(packet)
    db.commit()
    return PacketAckResponse(acked=True, packet_id=packet_id, status=payload.status)


@app.get("/health", response_model=HealthResponse)
def health(request: Request, db: Annotated[Session, Depends(get_db)]):
    _enforce_observability_mode(request, db, cfg.observability.health_mode)
    db.execute(select(1)).scalar_one()
    return HealthResponse(
        status="ok",
        time=utcnow(),
    )


@app.get("/stats", response_model=StatsResponse)
def stats(request: Request, db: Annotated[Session, Depends(get_db)]):
    _enforce_observability_mode(request, db, cfg.observability.stats_mode)
    now = utcnow()
    nodes_total = int(db.scalar(select(func.count()).select_from(Node)) or 0)
    endpoints_total = int(db.scalar(select(func.count()).select_from(Endpoint)) or 0)
    active_links_total = int(db.scalar(select(func.count()).select_from(Link).where(Link.status == "active")) or 0)
    queued_packets_total = int(
        db.scalar(
            select(func.count(Delivery.delivery_id))
            .select_from(Delivery)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(active_packet_clause(now=now))
        )
        or 0
    )
    queued_bytes_total = int(
        db.scalar(
            select(func.coalesce(func.sum(Packet.payload_bytes), 0))
            .select_from(Delivery)
            .join(Packet, Packet.packet_id == Delivery.packet_id)
            .where(active_packet_clause(now=now))
        )
        or 0
    )
    link_codes_active_total = int(
        db.scalar(select(func.count()).select_from(LinkCode).where(active_link_code_clause(now=now))) or 0
    )
    return StatsResponse(
        nodes_total=nodes_total,
        endpoints_total=endpoints_total,
        active_links_total=active_links_total,
        queued_packets_total=queued_packets_total,
        queued_bytes_total=queued_bytes_total,
        link_codes_active_total=link_codes_active_total,
        time=now,
    )
