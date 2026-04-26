from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit
import ipaddress
import logging

from arqs_api import ARQSClient, ARQSConnectionError, ARQSInsecureTransportError

from .types import TransportResolution


logger = logging.getLogger("arqs.appkit.transport")


class TransportResolver:
    def __init__(self, *, timeout: float = 5.0) -> None:
        self.timeout = float(timeout)

    def resolve(
        self,
        *,
        base_url: str,
        transport_policy: str = "prefer_https",
        transport_preferences: dict[str, str] | None = None,
    ) -> TransportResolution:
        normalized_url = str(base_url).strip()
        if not normalized_url:
            raise ValueError("config base_url is required")

        parsed = urlsplit(normalized_url)
        host_key = parsed.netloc.lower()
        remembered = dict(transport_preferences or {}).get(host_key)
        effective_policy = str(remembered or transport_policy or "prefer_https")

        probe_client = ARQSClient(
            normalized_url,
            transport_policy="allow_http",
            allow_local_http_auth=True,
            timeout=self.timeout,
        )
        probe = probe_client.probe_transport(normalized_url, timeout=self.timeout)
        http_reachable = bool(probe.http_attempt and probe.http_attempt.reachable)
        https_reachable = bool(probe.https_attempt and probe.https_attempt.reachable)
        is_local_http = _is_local_or_private_host(parsed.hostname)
        preference_updates: dict[str, str] = {}

        if effective_policy == "require_https":
            if not https_reachable or probe.normalized_https_base_url is None:
                raise ARQSInsecureTransportError("require_https is configured but HTTPS is not reachable")
            return TransportResolution(
                base_url=probe.normalized_https_base_url,
                transport_policy="require_https",
                allow_local_http_auth=False,
                classification=probe.classification,
                host_key=host_key,
                preference_updates=preference_updates,
            )

        if effective_policy == "allow_http":
            resolved = self._choose_allow_http_base_url(
                configured_base_url=normalized_url,
                probe=probe,
                http_reachable=http_reachable,
                https_reachable=https_reachable,
            )
            if urlsplit(resolved).scheme == "http":
                if host_key:
                    preference_updates[host_key] = "allow_http"
                if not is_local_http:
                    logger.warning("using authenticated HTTP transport for non-local host %s", host_key)
            return TransportResolution(
                base_url=resolved,
                transport_policy="allow_http",
                allow_local_http_auth=True,
                classification=probe.classification,
                host_key=host_key,
                preference_updates=preference_updates,
            )

        if https_reachable and probe.normalized_https_base_url is not None:
            return TransportResolution(
                base_url=probe.normalized_https_base_url,
                transport_policy="prefer_https",
                allow_local_http_auth=False,
                classification=probe.classification,
                host_key=host_key,
                preference_updates=preference_updates,
            )

        if http_reachable and probe.normalized_http_base_url is not None:
            if not is_local_http and remembered != "allow_http":
                raise ARQSInsecureTransportError(
                    "public HTTP-only transport is blocked under prefer_https; set allow_http explicitly for this host"
                )
            if host_key:
                preference_updates[host_key] = "allow_http"
            return TransportResolution(
                base_url=probe.normalized_http_base_url,
                transport_policy="allow_http",
                allow_local_http_auth=True,
                classification=probe.classification,
                host_key=host_key,
                preference_updates=preference_updates,
            )

        raise ARQSConnectionError("failed to reach ARQS server over either HTTP or HTTPS")

    def _choose_allow_http_base_url(
        self,
        *,
        configured_base_url: str,
        probe: Any,
        http_reachable: bool,
        https_reachable: bool,
    ) -> str:
        configured_scheme = urlsplit(configured_base_url).scheme.lower()
        if configured_scheme == "https" and https_reachable and probe.normalized_https_base_url is not None:
            return probe.normalized_https_base_url
        if configured_scheme == "http" and http_reachable and probe.normalized_http_base_url is not None:
            return probe.normalized_http_base_url
        if https_reachable and probe.normalized_https_base_url is not None:
            return probe.normalized_https_base_url
        if http_reachable and probe.normalized_http_base_url is not None:
            return probe.normalized_http_base_url
        raise ARQSConnectionError("failed to reach ARQS server with allow_http transport policy")


def _is_local_or_private_host(host: str | None) -> bool:
    normalized = str(host or "").strip().lower()
    if not normalized:
        return False
    if normalized in {"localhost", "localhost.localdomain"}:
        return True
    try:
        address = ipaddress.ip_address(normalized)
    except ValueError:
        return normalized.endswith(".local")
    return (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
    )


__all__ = ["TransportResolver"]
