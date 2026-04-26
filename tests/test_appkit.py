from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apis"))

from arqs_api import ARQSConnectionError, PacketSendResult  # noqa: E402
from appkit import ARQSApp, Notifier, TYPE_REACTION_V1  # noqa: E402
from appkit.outbox import SQLiteOutbox  # noqa: E402
from appkit.store import ContactBook, RuntimeStore  # noqa: E402


class _SuccessClient:
    def send_packet(self, **kwargs):
        return PacketSendResult(
            result="accepted",
            packet_id=uuid.UUID(str(kwargs["packet_id"])),
            delivery_id=uuid.uuid4(),
            expires_at=None,
        )


class _FailingClient:
    def send_packet(self, **kwargs):
        raise ARQSConnectionError("temporary failure")


class _CaptureClient:
    def __init__(self) -> None:
        self.calls = []

    def send_packet(self, **kwargs):
        self.calls.append(kwargs)
        return PacketSendResult(
            result="accepted",
            packet_id=uuid.uuid4(),
            delivery_id=None,
            expires_at=None,
        )


class AppKitTests(unittest.TestCase):
    def test_contact_book_round_trips_contact(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = RuntimeStore("backup-monitor", state_root=tmpdir)
            book = ContactBook(store)
            contact = book.upsert(
                label="phone",
                local_endpoint_id=str(uuid.uuid4()),
                remote_endpoint_id=str(uuid.uuid4()),
                link_id=str(uuid.uuid4()),
            )

            loaded = book.get("phone")

            self.assertIsNotNone(loaded)
            self.assertEqual(contact.label, loaded.label)
            self.assertEqual(contact.remote_endpoint_id, loaded.remote_endpoint_id)

    def test_outbox_dead_letters_none_retry_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = SQLiteOutbox(Path(tmpdir) / "outbox.sqlite3")
            entry = outbox.enqueue(
                from_endpoint_id=str(uuid.uuid4()),
                to_endpoint_id=str(uuid.uuid4()),
                headers={"arqs_type": "notification.v1"},
                body="hello",
                data={"ok": True},
                meta={},
                retry_policy="none",
                max_attempts=20,
                expires_after_seconds=3600,
            )

            result = outbox.flush_packet(_FailingClient(), entry.packet_id)

            self.assertEqual("dead_letter", result.status)
            dead_letters = outbox.list_dead_letters()
            self.assertEqual(1, len(dead_letters))
            self.assertEqual(entry.packet_id, dead_letters[0].packet_id)

    def test_outbox_retries_forever_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = SQLiteOutbox(Path(tmpdir) / "outbox.sqlite3")
            entry = outbox.enqueue(
                from_endpoint_id=str(uuid.uuid4()),
                to_endpoint_id=str(uuid.uuid4()),
                headers={"arqs_type": "notification.v1"},
                body="hello",
                data={"ok": True},
                meta={},
                retry_policy="forever",
                max_attempts=1,
                expires_after_seconds=1,
            )

            result = outbox.flush_packet(_FailingClient(), entry.packet_id)
            queued_entry = outbox.get_by_packet_id(entry.packet_id)

            self.assertEqual("queued", result.status)
            self.assertIsNotNone(queued_entry)
            self.assertIsNone(queued_entry.max_attempts)
            self.assertIsNone(queued_entry.expires_at)

    def test_outbox_success_flushes_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            outbox = SQLiteOutbox(Path(tmpdir) / "outbox.sqlite3")
            entry = outbox.enqueue(
                from_endpoint_id=str(uuid.uuid4()),
                to_endpoint_id=str(uuid.uuid4()),
                headers={"arqs_type": "notification.v1"},
                body="hello",
                data={"ok": True},
                meta={},
                retry_policy="bounded",
                max_attempts=3,
                expires_after_seconds=3600,
            )

            result = outbox.flush_packet(_SuccessClient(), entry.packet_id)

            self.assertEqual("accepted", result.status)
            self.assertIsNone(outbox.get_by_packet_id(entry.packet_id))

    def test_import_surface_exposes_notifier(self) -> None:
        self.assertEqual("Notifier", Notifier.__name__)

    def test_send_reaction_builds_standard_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            app = ARQSApp.for_app("reactor", state_root=tmpdir)
            client = _CaptureClient()
            app.client = client
            original_packet_id = "f70d9b16-cd56-4616-bb6b-c30ddc720a47"
            from_endpoint_id = str(uuid.uuid4())
            to_endpoint_id = str(uuid.uuid4())

            result = app.send_reaction(
                for_packet_id=original_packet_id,
                action="set",
                emoji="🔥",
                emoji_name="fire",
                source_platform="discord",
                source_user_id="123456789",
                source_message_id="987654321",
                reaction_id="9b777b0e-6d7a-40b1-9c39-885d7bbd76a1",
                reacted_at="2026-04-26T12:00:00Z",
                from_endpoint_id=from_endpoint_id,
                to_endpoint_id=to_endpoint_id,
                delivery_mode="direct",
            )

        self.assertEqual("accepted", result.status)
        self.assertEqual(1, len(client.calls))
        call = client.calls[0]
        self.assertEqual(TYPE_REACTION_V1, call["headers"]["arqs_type"])
        self.assertEqual(original_packet_id, call["headers"]["causation_id"])
        self.assertEqual("reacted with 🔥", call["body"])
        self.assertEqual("set", call["data"]["action"])
        self.assertTrue(call["data"]["reaction_key"].startswith("reaction.v1:"))
        self.assertEqual("fire", call["data"]["emoji_name"])
        self.assertEqual("discord", call["data"]["source_platform"])
        self.assertEqual("123456789", call["data"]["source_user_id"])
        self.assertEqual("987654321", call["data"]["source_message_id"])


if __name__ == "__main__":
    unittest.main()
