from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from arqs_api import ARQSClient


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def touch(path: Path | None) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("ok\n", encoding="utf-8")


def cmd_poll(args: argparse.Namespace) -> int:
    client = ARQSClient.from_identity_file(args.base_url, args.identity_path)
    try:
        deliveries = client.poll_inbox(wait=args.wait_seconds, limit=100, request_timeout=max(5.0, float(args.wait_seconds) + 5.0))
    except Exception as exc:
        write_json(
            args.output_path,
            {
                "status": "error",
                "error": str(exc),
            },
        )
        return 0

    serial = []
    for delivery in deliveries:
        serial.append(
            {
                "delivery_id": str(delivery.delivery_id),
                "packet_id": str(delivery.packet.packet_id),
                "body": delivery.packet.body,
                "from_endpoint_id": str(delivery.packet.from_endpoint_id),
                "to_endpoint_id": str(delivery.packet.to_endpoint_id),
                "state": delivery.state,
            }
        )

    payload = {
        "status": "ok",
        "count": len(serial),
        "deliveries": serial,
    }
    write_json(args.output_path, payload)

    if serial:
        touch(args.checkpoint_path)
        if args.ack:
            for item in serial:
                client.ack_delivery(item["delivery_id"], status="handled")
        if args.hold_after_receive:
            while True:
                time.sleep(1.0)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="ARQS downtime test worker")
    sub = parser.add_subparsers(dest="command", required=True)

    poll = sub.add_parser("poll", help="poll inbox once")
    poll.add_argument("--base-url", required=True)
    poll.add_argument("--identity-path", type=Path, required=True)
    poll.add_argument("--wait-seconds", type=int, default=10)
    poll.add_argument("--output-path", type=Path, required=True)
    poll.add_argument("--checkpoint-path", type=Path)
    poll.add_argument("--ack", action="store_true")
    poll.add_argument("--hold-after-receive", action="store_true")
    poll.set_defaults(func=cmd_poll)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
