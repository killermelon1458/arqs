from __future__ import annotations

from socket import gethostname
from traceback import format_exception
from typing import Any
import uuid

from arqs_conventions import TYPE_NOTIFICATION_V1, TYPE_SCRIPT_FAILURE_TRACEBACK_V1, TYPE_SCRIPT_FAILURE_V1

from .app import ARQSApp
from .store import to_iso, utc_now
from .types import NotificationPayload, SendResult


_NOTIFIER_CACHE: dict[tuple[str, str | None], "Notifier"] = {}


class Notifier:
    def __init__(self, app: ARQSApp) -> None:
        self.app = app

    @classmethod
    def for_app(
        cls,
        app_name: str,
        *,
        state_root: str | None = None,
        **config_overrides: Any,
    ) -> "Notifier":
        return cls(ARQSApp.for_app(app_name, state_root=state_root, **config_overrides))

    def send_notification(
        self,
        title: str,
        body: str,
        *,
        level: str = "info",
        data: dict[str, Any] | None = None,
        contact: str | None = None,
        delivery_mode: str | None = None,
        retry_policy: str | None = None,
        dedupe_key: str | None = None,
        dedupe_window_seconds: int | None = None,
        priority: str | None = None,
        tags: list[str] | None = None,
        script: str | None = None,
    ) -> SendResult:
        created_at = utc_now()
        payload = NotificationPayload(
            notification_id=str(uuid.uuid4()),
            title=str(title),
            body=str(body),
            level=str(level),
            created_at=created_at,
            source=self.app.app_name,
            host=gethostname(),
            script=None if script in (None, "") else str(script),
            tags=tuple(tags or ()),
            priority=None if priority in (None, "") else str(priority),
            dedupe_key=None if dedupe_key in (None, "") else str(dedupe_key),
            extra_data=dict(data or {}),
        )
        notification_data = dict(payload.extra_data)
        notification_data.update(
            {
                "body": payload.body,
                "created_at": to_iso(payload.created_at),
                "dedupe_key": payload.dedupe_key,
                "dedupe_window_seconds": dedupe_window_seconds,
                "host": payload.host,
                "level": payload.level,
                "notification_id": payload.notification_id,
                "priority": payload.priority,
                "script": payload.script,
                "source": payload.source,
                "tags": list(payload.tags),
                "title": payload.title,
            }
        )
        return self.app.send_type(
            arqs_type=TYPE_NOTIFICATION_V1,
            body=f"{title}: {body}",
            data=notification_data,
            contact=contact,
            delivery_mode=delivery_mode,
            retry_policy=retry_policy,
            content_type="application/json",
        )

    def send_script_success(
        self,
        *,
        script: str,
        summary: str,
        data: dict[str, Any] | None = None,
        contact: str | None = None,
        delivery_mode: str | None = None,
    ) -> SendResult:
        success_data = dict(data or {})
        success_data["script"] = str(script)
        success_data["status"] = "success"
        return self.send_notification(
            title=f"{script} succeeded",
            body=summary,
            level="success",
            data=success_data,
            contact=contact,
            delivery_mode=delivery_mode,
            script=script,
        )

    def send_script_failure(
        self,
        *,
        script: str,
        exc: BaseException,
        include_traceback: bool = True,
        contact: str | None = None,
        delivery_mode: str | None = None,
    ) -> SendResult:
        failure_id = str(uuid.uuid4())
        created_at = utc_now()
        traceback_text = "".join(format_exception(exc)) if include_traceback else ""
        error_body = f"{script} failed: {exc}"
        failure_data = {
            "created_at": to_iso(created_at),
            "error_message": str(exc),
            "error_type": exc.__class__.__name__,
            "failure_id": failure_id,
            "script": str(script),
            "traceback_encoding": "plain" if include_traceback else None,
            "traceback_included": bool(include_traceback),
        }
        arqs_type = TYPE_SCRIPT_FAILURE_TRACEBACK_V1 if include_traceback else TYPE_SCRIPT_FAILURE_V1
        return self.app.send_type(
            arqs_type=arqs_type,
            body=traceback_text or error_body,
            data=failure_data,
            contact=contact,
            delivery_mode=delivery_mode,
            content_type="application/json",
        )


def notifier(app_name: str, *, state_root: str | None = None, **config_overrides: Any) -> Notifier:
    cache_key = (str(app_name), state_root)
    cached = _NOTIFIER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    created = Notifier.for_app(app_name, state_root=state_root, **config_overrides)
    _NOTIFIER_CACHE[cache_key] = created
    return created


__all__ = ["Notifier", "notifier"]
