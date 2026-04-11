from __future__ import annotations

import ipaddress
from functools import lru_cache

from fastapi import Request

from .config import settings


@lru_cache(maxsize=1)
def _trusted_networks() -> tuple[ipaddress._BaseNetwork, ...]:
    networks = []
    for raw in settings.trusted_proxy_cidrs.split(","):
        raw = raw.strip()
        if not raw:
            continue
        networks.append(ipaddress.ip_network(raw, strict=False))
    return tuple(networks)


@lru_cache(maxsize=1)
def _trusted_ips() -> set[str]:
    return {item.strip() for item in settings.trusted_proxy_ips.split(",") if item.strip()}


def _remote_host_trusted(remote_host: str | None) -> bool:
    if not remote_host:
        return False
    if remote_host in _trusted_ips():
        return True
    try:
        ip = ipaddress.ip_address(remote_host)
    except ValueError:
        return False
    return any(ip in network for network in _trusted_networks())


def _first_header_value(value: str | None) -> str | None:
    if not value:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def resolve_source_identifier(request: Request) -> str:
    direct_host = request.client.host if request.client else "unknown"
    if settings.trust_proxy_headers and _remote_host_trusted(direct_host):
        candidate = _first_header_value(request.headers.get("cf-connecting-ip"))
        if candidate:
            return candidate
        candidate = _first_header_value(request.headers.get("x-forwarded-for"))
        if candidate:
            return candidate
    return direct_host or "unknown"
