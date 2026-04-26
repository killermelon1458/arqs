from __future__ import annotations

from argparse import ArgumentParser, Namespace
from typing import Any
import json

from .app import ARQSApp
from .notifier import Notifier


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog="python -m appkit", description="ARQS AppKit CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    setup = subparsers.add_parser("setup", help="create or update app configuration")
    setup.add_argument("--app", required=True)
    setup.add_argument("--base-url", required=True)
    setup.add_argument("--default-contact")
    setup.add_argument("--node-name")
    setup.add_argument("--default-endpoint-name")
    setup.add_argument("--default-endpoint-kind")
    setup.add_argument("--transport-policy")
    setup.set_defaults(func=_run_setup)

    request_link = subparsers.add_parser("request-link", help="request a link code from the current default endpoint")
    request_link.add_argument("--app", required=True)
    request_link.add_argument("--requested-mode", default="bidirectional")
    request_link.set_defaults(func=_run_request_link)

    redeem_link = subparsers.add_parser("redeem-link", help="redeem a link code and save a contact")
    redeem_link.add_argument("--app", required=True)
    redeem_link.add_argument("code")
    redeem_link.add_argument("--label", required=True)
    redeem_link.set_defaults(func=_run_redeem_link)

    contacts = subparsers.add_parser("contacts", help="list contacts")
    contacts.add_argument("--app", required=True)
    contacts.set_defaults(func=_run_contacts)

    test_notification = subparsers.add_parser("test-notification", help="send a test notification")
    test_notification.add_argument("--app", required=True)
    test_notification.add_argument("--title", required=True)
    test_notification.add_argument("--body", required=True)
    test_notification.add_argument("--contact")
    test_notification.set_defaults(func=_run_test_notification)

    flush_outbox = subparsers.add_parser("flush-outbox", help="flush queued outbox packets")
    flush_outbox.add_argument("--app", required=True)
    flush_outbox.set_defaults(func=_run_flush_outbox)

    dead_letter = subparsers.add_parser("dead-letter", help="inspect dead-lettered packets")
    dead_letter.add_argument("--app", required=True)
    dead_letter.add_argument("--limit", type=int, default=20)
    dead_letter.set_defaults(func=_run_dead_letter)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


def _app_from_args(args: Namespace, **overrides: Any) -> ARQSApp:
    return ARQSApp.for_app(str(args.app), **overrides)


def _run_setup(args: Namespace) -> int:
    app = _app_from_args(
        args,
        base_url=args.base_url,
        default_contact=args.default_contact,
        node_name=args.node_name,
        default_endpoint_name=args.default_endpoint_name,
        default_endpoint_kind=args.default_endpoint_kind,
        transport_policy=args.transport_policy,
    )
    app.setup()
    print(app.store.paths.state_dir)
    return 0


def _run_request_link(args: Namespace) -> int:
    app = _app_from_args(args)
    link_code = app.request_link_code(requested_mode=args.requested_mode)
    print(link_code.code)
    return 0


def _run_redeem_link(args: Namespace) -> int:
    app = _app_from_args(args)
    contact = app.redeem_link_code(args.code, label=args.label)
    print(json.dumps({"label": contact.label, "remote_endpoint_id": contact.remote_endpoint_id}, indent=2))
    return 0


def _run_contacts(args: Namespace) -> int:
    app = _app_from_args(args)
    payload = [
        {
            "label": contact.label,
            "local_endpoint_id": contact.local_endpoint_id,
            "remote_endpoint_id": contact.remote_endpoint_id,
            "link_id": contact.link_id,
            "status": contact.status,
        }
        for contact in app.list_contacts()
    ]
    print(json.dumps(payload, indent=2))
    return 0


def _run_test_notification(args: Namespace) -> int:
    note = Notifier.for_app(str(args.app))
    result = note.send_notification(title=args.title, body=args.body, contact=args.contact)
    print(json.dumps({"packet_id": result.packet_id, "status": result.status}, indent=2))
    return 0


def _run_flush_outbox(args: Namespace) -> int:
    app = _app_from_args(args)
    results = app.flush_outbox()
    print(json.dumps([{"packet_id": item.packet_id, "status": item.status} for item in results], indent=2))
    return 0


def _run_dead_letter(args: Namespace) -> int:
    app = _app_from_args(args)
    entries = app.outbox.list_dead_letters(limit=args.limit)
    print(
        json.dumps(
            [
                {
                    "packet_id": entry.packet_id,
                    "status": entry.status,
                    "attempts": entry.attempts,
                    "last_error": entry.last_error,
                }
                for entry in entries
            ],
            indent=2,
        )
    )
    return 0


__all__ = ["build_parser", "main"]
