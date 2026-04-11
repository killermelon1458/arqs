from __future__ import annotations

from fastapi import Header, HTTPException, status

from .config import settings
from .db import fetch_one, json_loads
from .security import hash_api_key


def _parse_bearer(authorization: str | None) -> str:
    if not authorization:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing authorization header")
    parts = authorization.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token")
    return parts[1].strip()


def require_actor(authorization: str | None = Header(default=None)) -> dict:
    api_key = _parse_bearer(authorization)
    actor = fetch_one(
        """
        SELECT actor_id, actor_type, capabilities_json, adapter_type, state, display_name
        FROM actors
        WHERE api_key_hash = ?
        """,
        (hash_api_key(api_key),),
    )
    if not actor or actor["state"] != "active":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid actor api key")
    actor["capabilities"] = json_loads(actor["capabilities_json"])
    client = fetch_one("SELECT client_id FROM clients WHERE owner_actor_id = ?", (actor["actor_id"],))
    actor["client_id"] = client["client_id"] if client else None
    return actor


def require_admin(authorization: str | None = Header(default=None)) -> None:
    token = _parse_bearer(authorization)
    if token != settings.admin_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin api key")


def require_capability(actor: dict, capability: str) -> None:
    if capability not in actor["capabilities"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=f"missing capability: {capability}")
