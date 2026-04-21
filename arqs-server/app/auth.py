from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import SessionLocal, get_config, get_db
from .models import Node

PBKDF2_ITERATIONS = 390_000


def generate_key_id() -> str:
    return str(uuid.uuid4())


def generate_api_key(key_id: str | None = None) -> tuple[str, str]:
    actual_key_id = key_id or generate_key_id()
    secret = secrets.token_urlsafe(32)
    return actual_key_id, f"arqs_{actual_key_id}_{secret}"


def extract_key_id(api_key: str) -> str | None:
    value = api_key.strip()
    if not value.startswith("arqs_"):
        return None
    parts = value.split("_", 2)
    if len(parts) != 3:
        return None
    _, key_id, secret = parts
    if not key_id or not secret:
        return None
    try:
        uuid.UUID(key_id)
    except ValueError:
        return None
    return key_id


def hash_api_key(api_key: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", api_key.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "$".join(
        [
            "pbkdf2_sha256",
            str(PBKDF2_ITERATIONS),
            base64.b64encode(salt).decode("ascii"),
            base64.b64encode(digest).decode("ascii"),
        ]
    )


def verify_api_key(api_key: str, stored: str) -> bool:
    try:
        scheme, iterations_str, salt_b64, digest_b64 = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        iterations = int(iterations_str)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", api_key.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def get_client_ip(request: Request) -> str:
    cfg = get_config()
    direct_ip = request.client.host if request.client else "unknown"
    try:
        direct_addr = ipaddress.ip_address(direct_ip)
    except ValueError:
        return direct_ip

    trusted = False
    for candidate in cfg.network.trusted_proxies:
        try:
            if "/" in candidate:
                if direct_addr in ipaddress.ip_network(candidate, strict=False):
                    trusted = True
                    break
            elif direct_addr == ipaddress.ip_address(candidate):
                trusted = True
                break
        except ValueError:
            continue

    if not trusted:
        return direct_ip

    for header_name in cfg.network.trusted_forwarded_headers:
        raw = request.headers.get(header_name)
        if not raw:
            continue
        if header_name.lower() == "x-forwarded-for":
            return raw.split(",", 1)[0].strip()
        return raw.strip()
    return direct_ip


ApiKeyHeader = Annotated[str | None, Header(alias="X-ARQS-API-Key", convert_underscores=False)]
AuthorizationHeader = Annotated[str | None, Header(alias="Authorization")]


@dataclass(frozen=True)
class AuthenticatedNodeContext:
    node_id: str
    status: str


def _extract_api_key(x_arqs_api_key: str | None, authorization: str | None) -> str:
    if x_arqs_api_key:
        return x_arqs_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key")


def _authenticate_node(request: Request, db: Session, api_key: str) -> Node:
    key_id = extract_key_id(api_key)
    if key_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

    node = db.scalar(select(Node).where(Node.key_id == key_id))
    if node is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

    if not verify_api_key(api_key, node.api_key_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")

    if node.status == "disabled":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node disabled")
    if node.status == "revoked":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node revoked")
    if node.node_id in get_config().blacklist.node_ids:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node blacklisted")

    request.state.node_id = node.node_id
    return node


def require_node(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    x_arqs_api_key: ApiKeyHeader = None,
    authorization: AuthorizationHeader = None,
) -> Node:
    api_key = _extract_api_key(x_arqs_api_key, authorization)
    return _authenticate_node(request, db, api_key)


def require_node_context(
    request: Request,
    x_arqs_api_key: ApiKeyHeader = None,
    authorization: AuthorizationHeader = None,
) -> AuthenticatedNodeContext:
    api_key = _extract_api_key(x_arqs_api_key, authorization)
    with SessionLocal() as db:
        node = _authenticate_node(request, db, api_key)
        return AuthenticatedNodeContext(node_id=node.node_id, status=node.status)
