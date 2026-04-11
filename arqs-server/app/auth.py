from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_config, get_db
from .models import Node

PBKDF2_ITERATIONS = 390_000


def generate_api_key() -> str:
    return f"arqs_{secrets.token_urlsafe(32)}"


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


def _extract_api_key(x_arqs_api_key: str | None, authorization: str | None) -> str:
    if x_arqs_api_key:
        return x_arqs_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing api key")


def require_node(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    x_arqs_api_key: ApiKeyHeader = None,
    authorization: AuthorizationHeader = None,
) -> Node:
    api_key = _extract_api_key(x_arqs_api_key, authorization)
    for node in db.execute(select(Node)).scalars():
        if verify_api_key(api_key, node.api_key_hash):
            if node.status == "disabled":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node disabled")
            if node.status == "revoked":
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node revoked")
            if node.node_id in get_config().blacklist.node_ids:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="node blacklisted")
            request.state.node_id = node.node_id
            return node
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid api key")
