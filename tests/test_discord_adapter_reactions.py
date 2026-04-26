from __future__ import annotations

from pathlib import Path
import sys
import tempfile
import unittest
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "apis"))

from adapters.arqs_discord_appkit_bot import DiscordBridgeState, reaction_matches  # noqa: E402
from arqs_conventions import build_reaction_key  # noqa: E402


class DiscordAdapterReactionStateTests(unittest.TestCase):
    def test_outbound_message_reaction_state_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "discord_state.json"
            state = DiscordBridgeState(path)
            packet_id = str(uuid.uuid4())
            state.remember_outbound_message(
                packet_id=packet_id,
                discord_user_id="123",
                discord_message_id="456",
                discord_channel_id="789",
                binding_id="binding-1",
            )
            state.mark_outbound_receipt(packet_id, "read")
            fire_key = build_reaction_key(
                for_packet_id=packet_id,
                source_platform="discord",
                source_user_id="123",
                emoji="🔥",
                emoji_name="fire",
            )
            thumbs_key = build_reaction_key(
                for_packet_id=packet_id,
                source_platform="discord",
                source_user_id="123",
                emoji="👍",
                emoji_name="thumbs_up",
            )
            state.set_outbound_reaction(
                packet_id,
                fire_key,
                {"emoji": "🔥", "emoji_name": "fire", "animated": False},
            )
            state.set_outbound_reaction(
                packet_id,
                thumbs_key,
                {"emoji": "👍", "emoji_name": "thumbs_up", "animated": False},
            )
            state.remove_outbound_reaction(packet_id, fire_key)
            state.save()

            reloaded = DiscordBridgeState(path)
            stored = reloaded.get_outbound_message(packet_id)

        self.assertIsNotNone(stored)
        self.assertTrue(stored["has_read_receipt"])
        self.assertNotIn(fire_key, stored["active_reactions"])
        self.assertEqual("👍", stored["active_reactions"][thumbs_key]["emoji"])
        self.assertTrue(reaction_matches(stored["active_reactions"][thumbs_key], {"emoji": "👍"}))


if __name__ == "__main__":
    unittest.main()
