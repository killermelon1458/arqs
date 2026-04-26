from __future__ import annotations

import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apis"))

from arqs_conventions import (  # noqa: E402
    HEADER_CAUSATION_ID,
    HEADER_CORRELATION_ID,
    HEADER_RECEIPT_REQUEST,
    TYPE_COMMAND_V1,
    TYPE_MESSAGE_V1,
    TYPE_PRESENCE_PING_V1,
    TYPE_PRESENCE_PONG_V1,
    TYPE_REACTION_V1,
    TYPE_RECEIPT_RECEIVED_V1,
    build_reaction_key,
    build_receipt_headers,
    build_v1_headers,
    get_causation_id,
    get_correlation_id,
    get_reaction_key,
    get_receipt_request,
    is_presence_ping_type,
    is_presence_pong_type,
    is_receipt_type,
    render_packet_text,
    should_ignore_receipt_request,
)


class ConventionHelpersTests(unittest.TestCase):
    def test_build_v1_headers_without_optional_relationship_fields(self) -> None:
        headers = build_v1_headers(TYPE_MESSAGE_V1, content_type="text/plain; charset=utf-8")

        self.assertEqual(headers["arqs_envelope"], "v1")
        self.assertEqual(headers["arqs_type"], TYPE_MESSAGE_V1)
        self.assertNotIn(HEADER_RECEIPT_REQUEST, headers)
        self.assertNotIn(HEADER_CORRELATION_ID, headers)
        self.assertNotIn(HEADER_CAUSATION_ID, headers)

    def test_build_v1_headers_adds_official_optional_fields_and_preserves_private_headers(self) -> None:
        correlation_id = "9b777b0e-6d7a-40b1-9c39-885d7bbd76a1"
        causation_id = uuid.UUID("f70d9b16-cd56-4616-bb6b-c30ddc720a47")

        headers = build_v1_headers(
            TYPE_COMMAND_V1,
            content_type="application/json",
            receipt_request=["received", "processed", ""],
            correlation_id=correlation_id,
            causation_id=causation_id,
            extra_headers={
                "arqs_type": "override-me",
                HEADER_CORRELATION_ID: "not-a-uuid",
                HEADER_RECEIPT_REQUEST: ["bad"],
                "x-private-header": "kept",
            },
        )

        self.assertEqual(headers["arqs_type"], TYPE_COMMAND_V1)
        self.assertEqual(headers[HEADER_RECEIPT_REQUEST], ["received", "processed"])
        self.assertEqual(headers[HEADER_CORRELATION_ID], correlation_id)
        self.assertEqual(headers[HEADER_CAUSATION_ID], str(causation_id))
        self.assertEqual(headers["x-private-header"], "kept")

    def test_build_v1_headers_rejects_invalid_correlation_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid UUID header value"):
            build_v1_headers(
                TYPE_MESSAGE_V1,
                content_type="text/plain; charset=utf-8",
                correlation_id="definitely-not-a-uuid",
            )

    def test_build_v1_headers_rejects_invalid_causation_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "invalid UUID header value"):
            build_v1_headers(
                TYPE_MESSAGE_V1,
                content_type="text/plain; charset=utf-8",
                causation_id="definitely-not-a-uuid",
            )

    def test_getters_normalize_uuid_fields_and_ignore_invalid_receipt_request_shapes(self) -> None:
        correlation_id = uuid.UUID("9b777b0e-6d7a-40b1-9c39-885d7bbd76a1")
        causation_id = uuid.UUID("f70d9b16-cd56-4616-bb6b-c30ddc720a47")

        headers = {
            HEADER_CORRELATION_ID: str(correlation_id).upper(),
            HEADER_CAUSATION_ID: str(causation_id).upper(),
            HEADER_RECEIPT_REQUEST: "received",
        }

        self.assertEqual(get_correlation_id(headers), str(correlation_id))
        self.assertEqual(get_causation_id(headers), str(causation_id))
        self.assertEqual(get_receipt_request(headers), ())

    def test_type_classification_helpers_match_receipt_and_presence_semantics(self) -> None:
        receipt_headers = {"arqs_type": TYPE_RECEIPT_RECEIVED_V1}
        ping_headers = {"arqs_type": TYPE_PRESENCE_PING_V1}
        pong_headers = {"arqs_type": TYPE_PRESENCE_PONG_V1}

        self.assertTrue(is_receipt_type(TYPE_RECEIPT_RECEIVED_V1))
        self.assertTrue(should_ignore_receipt_request(receipt_headers))
        self.assertTrue(is_presence_ping_type(ping_headers["arqs_type"]))
        self.assertFalse(is_presence_pong_type(ping_headers["arqs_type"]))
        self.assertFalse(is_presence_ping_type(pong_headers["arqs_type"]))
        self.assertTrue(is_presence_pong_type(pong_headers["arqs_type"]))

    def test_build_receipt_headers_preserves_correlation_and_sets_causation(self) -> None:
        original_headers = build_v1_headers(
            TYPE_COMMAND_V1,
            content_type="application/json",
            receipt_request=["received"],
            correlation_id="9b777b0e-6d7a-40b1-9c39-885d7bbd76a1",
        )

        receipt_headers = build_receipt_headers(
            TYPE_RECEIPT_RECEIVED_V1,
            original_headers=original_headers,
            original_packet_id="f70d9b16-cd56-4616-bb6b-c30ddc720a47",
        )

        self.assertEqual(receipt_headers["arqs_type"], TYPE_RECEIPT_RECEIVED_V1)
        self.assertEqual(receipt_headers["content_type"], "application/json")
        self.assertEqual(receipt_headers[HEADER_CORRELATION_ID], original_headers[HEADER_CORRELATION_ID])
        self.assertEqual(receipt_headers[HEADER_CAUSATION_ID], "f70d9b16-cd56-4616-bb6b-c30ddc720a47")
        self.assertNotIn(HEADER_RECEIPT_REQUEST, receipt_headers)

    def test_render_reaction_packet_fallback_text(self) -> None:
        headers = build_v1_headers(TYPE_REACTION_V1, content_type="application/json")

        rendered = render_packet_text(
            body=None,
            data={"action": "set", "emoji": "🔥", "emoji_name": "fire"},
            headers=headers,
        )

        self.assertEqual(rendered, "reacted with 🔥")

    def test_render_reaction_packet_custom_emoji_name_fallback(self) -> None:
        headers = build_v1_headers(TYPE_REACTION_V1, content_type="application/json")

        rendered = render_packet_text(
            body=None,
            data={"action": "remove", "emoji_name": "party_blob"},
            headers=headers,
        )

        self.assertEqual(rendered, "removed reaction :party_blob:")

    def test_reaction_key_is_stable_for_same_message_user_and_emoji(self) -> None:
        first = build_reaction_key(
            for_packet_id="f70d9b16-cd56-4616-bb6b-c30ddc720a47",
            source_platform="discord",
            source_user_id="123456789",
            emoji="🔥",
            emoji_name="fire",
        )
        second = get_reaction_key(
            {
                "for_packet_id": "f70d9b16-cd56-4616-bb6b-c30ddc720a47",
                "source_platform": "discord",
                "source_user_id": "123456789",
                "emoji": "🔥",
                "emoji_name": "fire",
            }
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("reaction.v1:"))

    def test_reaction_key_distinguishes_different_emoji(self) -> None:
        fire = build_reaction_key(
            for_packet_id="f70d9b16-cd56-4616-bb6b-c30ddc720a47",
            source_platform="discord",
            source_user_id="123456789",
            emoji="🔥",
            emoji_name="fire",
        )
        thumbs_up = build_reaction_key(
            for_packet_id="f70d9b16-cd56-4616-bb6b-c30ddc720a47",
            source_platform="discord",
            source_user_id="123456789",
            emoji="👍",
            emoji_name="thumbs_up",
        )

        self.assertNotEqual(fire, thumbs_up)


if __name__ == "__main__":
    unittest.main()
