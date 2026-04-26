ARQS Packet Header Convention Draft

Purpose

This document defines a proposed v1 convention for ARQS packet headers.

ARQS itself remains a payload-agnostic relay. The server should not use these headers for routing, authorization, delivery decisions, key discovery, receipt handling, encryption handling, decompression, or application behavior. These headers exist so ARQS clients, adapters, and helper libraries can interpret packets consistently.

The server’s job remains:

receive authenticated packet
store packet
relay packet to the linked destination endpoint
allow transport ACK
expire/delete according to server policy

The client’s job is:

read headers
decode/decompress/decrypt if needed
interpret arqs_type
handle receipts, notifications, messages, blobs, commands, etc.

Core Packet Shape

An ARQS packet uses the existing packet fields:

{
  "headers": {},
  "body": null,
  "data": {},
  "meta": {}
}

Recommended meaning:

headers = how to interpret/decode the packet
data    = structured machine-readable payload facts
body    = human-readable text or encoded payload string
meta    = diagnostics/client/adapter context

Headers should stay small. Business payload should go in data or body, not in headers.

Required v1 Header Fields

Convention-compliant v1 packets should include these header fields:

{
  "arqs_envelope": "v1",
  "arqs_type": "notification.v1",
  "content_type": "application/json",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none"
}

arqs_envelope

The version of the ARQS client-side header/envelope convention.

Example:

"arqs_envelope": "v1"

This should only change when the overall header convention changes.

arqs_type

The semantic packet type. This tells receiving clients what kind of packet this is.

Examples:

notification.v1
message.v1
script.failure.v1
script.failure.traceback.v1
receipt.received.v1
receipt.processed.v1
receipt.read.v1
reaction.v1
blob.manifest.v1
blob.chunk.v1
blob.receipt.v1
encrypted.v1
command.v1
command.response.v1
presence.ping.v1
presence.pong.v1
ping.v1

The type version is included in the value, not the key.

Good:

"arqs_type": "notification.v1"

Bad:

"arqs_type_v1": "notification"

content_type

A MIME-like description of the payload content.

Examples:

text/plain; charset=utf-8
application/json
application/octet-stream
application/vnd.arqs.receipt+json
application/vnd.arqs.blob-manifest+json
application/vnd.arqs.encrypted

arqs_type carries ARQS semantic meaning. content_type describes the underlying payload format.

content_transfer_encoding

How the packet body string should be converted into bytes.

ARQS Envelope v1 defines two supported values:

utf-8
base64

utf-8

The body is normal text stored directly as a JSON string.

Decode behavior:

body string -> UTF-8 bytes

Use for:

plain messages
tracebacks
small JSON/text payloads
human-readable notifications

base64

The body is a base64 string representing bytes.

Decode behavior:

body string -> base64 decode -> bytes

Use for:

file chunks
compressed data
encrypted ciphertext
binary blobs
images
serialized binary formats

Unknown content_transfer_encoding values are not part of the v1 convention. Clients may reject them, ignore the packet, or pass them to custom handlers.

content_encoding

Compression or content coding applied after transfer decoding.

Initial v1 values:

identity
gzip

identity

No compression/content coding is applied.

gzip

The bytes obtained after transfer decoding are gzip-compressed and should be decompressed before interpreting the payload.

Decode order example for compressed traceback:

base64 decode -> gzip decompress -> UTF-8 decode

Future possible values:

zstd
br

These are reserved ideas only unless later documented.

encryption

Encryption envelope or cipher convention applied to the payload.

Initial v1 value:

none

Reserved future examples:

x25519-xchacha20poly1305.v1
x25519-aes256gcm.v1
age.v1

If encryption is not none, the outer packet should avoid leaking sensitive inner semantics. For encrypted content, prefer a generic outer type such as:

"arqs_type": "encrypted.v1"

The real inner packet type should be inside the encrypted payload.

Optional Universal Header Fields

receipt_request

A list of client-level receipt types requested by the sender.

Example:

"receipt_request": ["received", "processed"]

Supported values:

received
  Receiver client obtained and durably accepted/saved the packet.

processed
  Receiver application or handler successfully processed the packet.

read
  A human user viewed/read the packet.

Rules:

Receipts must not request receipts.
If arqs_type starts with receipt., clients should ignore receipt_request.
Unsupported receipt types may be ignored.
Receipt requests are client-level behavior, not server-level ACK behavior.
The ARQS server must not interpret receipt_request.

correlation_id

A UUID used to group related packets together.

Useful for:

command/response chains
presence ping/pong exchanges
file transfers
multi-packet workflows
receipt chains
logs/tracing
conversation threads

Example:

"correlation_id": "9b777b0e-6d7a-40b1-9c39-885d7bbd76a1"

Rules:

A response packet should preserve the original packet's correlation_id when one exists.
A receipt packet should preserve the original packet's correlation_id when one exists.
Clients may generate a new correlation_id when starting a new workflow, such as a command request or presence ping.
Clients should not generate a new correlation_id for a simple receipt if the original packet did not have one.

causation_id

A UUID identifying the packet/event that caused this packet.

Useful for:

reply tracking
receipt tracking
command responses
workflow tracing

Example:

"causation_id": "uuid-of-original-packet"

Rules:

A command response should set causation_id to the command packet's packet_id.
A receipt should set causation_id to the original packet's packet_id.
A reaction should set causation_id to the packet_id of the message being reacted to.
A presence pong should set causation_id to the original presence ping packet's packet_id.
causation_id identifies the direct cause, not necessarily the whole workflow.

Versioning Rules

Do not put version numbers in every field name.

Good:

{
  "arqs_envelope": "v1",
  "arqs_type": "notification.v1"
}

Bad:

{
  "arqs_envelope_v1": true,
  "arqs_type_v1": "notification"
}

Version values at the level where meaning changes.

Thing	Version?	Example

Overall envelope convention	Yes	arqs_envelope: "v1"
Semantic packet type	Yes	arqs_type: "notification.v1"
Encryption suite/envelope	Yes	encryption: "x25519-xchacha20poly1305.v1"
Compression name	Usually no	content_encoding: "gzip"
Transfer encoding	Usually no	content_transfer_encoding: "base64"
MIME/content type	Usually no	content_type: "application/json"


If only the notification format changes, bump only the packet type:

{
  "arqs_envelope": "v1",
  "arqs_type": "notification.v2",
  "content_type": "application/json",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none"
}

If the whole header convention changes, bump the envelope:

{
  "arqs_envelope": "v2"
}

Plain Message Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "message.v1",
    "content_type": "text/plain; charset=utf-8",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "body": "Hello from ARQS",
  "data": {},
  "meta": {
    "client": "arqs_appkit"
  }
}

Notification Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "notification.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "body": "Disk space low: /mnt/docker is at 92%",
  "data": {
    "notification_id": "uuid",
    "title": "Disk space low",
    "body": "/mnt/docker is at 92%",
    "level": "warning",
    "created_at": "2026-04-24T13:00:00Z"
  },
  "meta": {
    "client": "system-health-monitor"
  }
}

Script Failure / Traceback Example

Plain traceback:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "script.failure.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "body": "Script failed: backup_job.py - RuntimeError: backup failed",
  "data": {
    "script": "backup_job.py",
    "error_type": "RuntimeError",
    "error_message": "backup failed",
    "traceback": "Traceback text here..."
  },
  "meta": {
    "client": "backup-monitor"
  }
}

Compressed traceback:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "script.failure.traceback.v1",
    "content_type": "text/plain; charset=utf-8",
    "content_transfer_encoding": "base64",
    "content_encoding": "gzip",
    "encryption": "none"
  },
  "body": "BASE64_GZIP_TRACEBACK_HERE",
  "data": {
    "script": "backup_job.py",
    "error_type": "RuntimeError",
    "error_message": "backup failed",
    "original_size_bytes": 12842
  },
  "meta": {
    "client": "backup-monitor"
  }
}

Receipt Examples

Received Receipt

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "receipt.received.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "causation_id": "uuid-of-original-packet"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "receipt_type": "received",
    "status": "ok",
    "received_at": "2026-04-24T13:00:00Z"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

If the original packet had a correlation_id, copy it:

{
  "correlation_id": "original-correlation-id",
  "causation_id": "uuid-of-original-packet"
}

Processed Receipt

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "receipt.processed.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "causation_id": "uuid-of-original-packet"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "receipt_type": "processed",
    "status": "success",
    "processed_at": "2026-04-24T13:01:00Z"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

Read Receipt

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "receipt.read.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "causation_id": "uuid-of-original-packet"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "receipt_type": "read",
    "read_at": "2026-04-24T13:05:00Z"
  },
  "meta": {
    "client": "arqs_messages_gui"
  }
}

Reaction Example

`reaction.v1` records that a user added or removed an emoji reaction to another ARQS packet. It is independent from `receipt.read.v1`: a client may send both for one user action, but the convention does not require every reaction to universally mean read.

Required `data` fields:

* `reaction_id`
* `reaction_key`
* `for_packet_id`
* `action`, with value `set` or `remove`
* `emoji_name`
* `source_platform`
* `source_user_id`
* `reacted_at`

`reaction_id` identifies this reaction event packet. `reaction_key` identifies the durable active reaction slot and must be stable across the matching `set` and `remove` packets.

Build `reaction_key` from:

for_packet_id + source_platform + source_user_id + emoji identity

Emoji identity uses this precedence:

emoji_id
emoji
emoji_name

`set` upserts the active reaction for `reaction_key`. `remove` deletes the active reaction for `reaction_key`. Multiple active reactions for the same `for_packet_id` are allowed, so adding a new emoji must not remove older emoji reactions. Duplicate `set` packets are idempotent, and duplicate or already-removed `remove` packets should be harmless.

Optional `data` fields:

* `emoji_id`
* `animated`
* `source_message_id`

Relationship headers should preserve the original packet's `correlation_id` when one is available and set `causation_id` to `for_packet_id`.

`emoji` should contain the portable Unicode glyph when one exists. Discord custom emoji may instead use `emoji_name`, `emoji_id`, and `animated`; other clients are not required to fetch Discord emoji assets and may display a text fallback such as `:party_blob:`.

For add:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "reaction.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "original-correlation-id",
    "causation_id": "uuid-of-original-packet"
  },
  "body": "reacted with 🔥",
  "data": {
    "reaction_id": "uuid",
    "reaction_key": "reaction.v1:stable-key",
    "for_packet_id": "uuid-of-original-packet",
    "action": "set",
    "emoji": "🔥",
    "emoji_name": "fire",
    "source_platform": "discord",
    "source_user_id": "123456789",
    "reacted_at": "2026-04-26T12:00:00Z"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

For removal:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "reaction.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "causation_id": "uuid-of-original-packet"
  },
  "body": "removed reaction :party_blob:",
  "data": {
    "reaction_id": "uuid",
    "reaction_key": "reaction.v1:same-stable-key-as-the-set-packet",
    "for_packet_id": "uuid-of-original-packet",
    "action": "remove",
    "emoji_name": "party_blob",
    "emoji_id": "discord-custom-emoji-id",
    "animated": false,
    "source_platform": "discord",
    "source_user_id": "123456789",
    "source_message_id": "discord-message-id",
    "reacted_at": "2026-04-26T12:05:00Z"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

Clients with reaction support may render a reaction UI. Other clients may display the body text, or fall back to their usual unknown packet rendering.

Presence Packet Types

Presence pings are separate from diagnostic ping.v1 traffic. Use presence.ping.v1 and presence.pong.v1 for runtime availability checks.

Presence Ping Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "presence.ping.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "ping-exchange-uuid"
  },
  "body": "presence ping",
  "data": {
    "ping_id": "uuid",
    "sent_at": "2026-04-24T13:00:00Z",
    "reply_requested": true,
    "nonce": "random-string",
    "max_hops": 1
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

Recommended behavior:

Use a short packet TTL, such as 30-120 seconds.
reply_requested defaults to true.
max_hops defaults to 1.
Clients must not auto-reply to stale pings.
Clients must not auto-reply unless presence response is explicitly allowed.

Presence Pong Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "presence.pong.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "same-ping-exchange-uuid",
    "causation_id": "original-presence-ping-packet-id"
  },
  "body": "presence pong",
  "data": {
    "pong_id": "uuid",
    "for_ping_id": "original-ping-id",
    "original_sent_at": "2026-04-24T13:00:00Z",
    "received_at": "2026-04-24T13:00:02Z",
    "responded_at": "2026-04-24T13:00:02Z",
    "status": "online",
    "nonce": "same-random-string"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

Rules:

Auto-reply only to presence.ping.v1.
Never auto-reply to presence.pong.v1.
Preserve the ping's correlation_id if present.
Set causation_id to the ping packet's packet_id.
Echo the ping nonce when one is provided.
Presence response must be opt-in.
If presence response is disabled, clients should usually silently ignore the ping.

Blob Manifest Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "blob.manifest.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "transfer-uuid"
  },
  "body": "backup.zip",
  "data": {
    "transfer_id": "transfer-uuid",
    "filename": "backup.zip",
    "size_bytes_raw": 10485760,
    "chunk_count": 64,
    "chunk_size_bytes_raw": 196608,
    "sha256_file_raw": "...",
    "encoding": "base64",
    "compression": "none",
    "encryption": "none"
  },
  "meta": {}
}

Blob Chunk Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "blob.chunk.v1",
    "content_type": "application/octet-stream",
    "content_transfer_encoding": "base64",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "transfer-uuid"
  },
  "body": "BASE64_CHUNK_HERE",
  "data": {
    "transfer_id": "transfer-uuid",
    "chunk_index": 0,
    "chunk_count": 64,
    "chunk_size_bytes_raw": 196608,
    "sha256_chunk_raw": "..."
  },
  "meta": {}
}

Blob Receipt Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "blob.receipt.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "transfer-uuid"
  },
  "body": null,
  "data": {
    "transfer_id": "transfer-uuid",
    "status": "complete",
    "received_chunks": 64,
    "missing_chunks": [],
    "sha256_verified": true
  },
  "meta": {}
}

If chunks are missing:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "blob.receipt.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "transfer-uuid"
  },
  "body": null,
  "data": {
    "transfer_id": "transfer-uuid",
    "status": "missing_chunks",
    "received_chunks": 61,
    "missing_chunks": [3, 7, 18],
    "sha256_verified": false
  },
  "meta": {}
}

Encrypted Packet Example

Outer packet:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "encrypted.v1",
    "content_type": "application/octet-stream",
    "content_transfer_encoding": "base64",
    "content_encoding": "identity",
    "encryption": "x25519-xchacha20poly1305.v1"
  },
  "body": "BASE64_CIPHERTEXT_HERE",
  "data": {
    "key_id": "optional-public-key-id",
    "nonce": "base64-nonce",
    "ciphertext_size_bytes": 1234
  },
  "meta": {}
}

Possible plaintext before encryption:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "message.v1",
    "content_type": "text/plain; charset=utf-8",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "body": "secret message",
  "data": {},
  "meta": {}
}

Encrypted packets should not expose sensitive inner packet details in outer headers.

Command Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "command.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "command-chain-uuid",
    "receipt_request": ["received", "processed"]
  },
  "body": "ping",
  "data": {
    "command_id": "uuid",
    "command": "ping",
    "args": {}
  },
  "meta": {
    "client": "server-agent"
  }
}

Command Response Example

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "command.response.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none",
    "correlation_id": "command-chain-uuid",
    "causation_id": "original-command-packet-id"
  },
  "body": "pong",
  "data": {
    "command_id": "uuid",
    "ok": true,
    "result": {
      "message": "pong"
    }
  },
  "meta": {
    "client": "server-agent"
  }
}

Transport ACK vs Client Receipt

ARQS transport ACK:
  Sent to the server with /packet_ack.
  Means the destination node accepted server delivery and the server may delete the delivery.
  Does not mean a remote application processed or displayed the packet.

Client receipt packet:
  Sent as a normal ARQS packet back to the original sender.
  Means a receiver endpoint/client is reporting received, processed, read, or failed status.

A packet can be transport-ACKed without any client receipt being sent.
A client receipt can fail to send even after the original packet was transport-ACKed.

Return Route Requirement

Receipts and presence pongs require a usable route back to the original sender.

Rules:

If the link is bidirectional, receipts and pongs can normally be sent back.
If the link is one-way, receipts and pongs require a separate reverse route.
Clients should not imply that receipt delivery is possible when no reverse route exists.
If sending a receipt or pong fails due to route/link problems, the client may place it in dead-letter or mark it as failed.

What Should Not Go In Headers

Bad:

{
  "headers": {
    "disk_percent": 92,
    "filename": "backup.zip",
    "traceback": "large traceback here"
  }
}

Good:

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "notification.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "data": {
    "disk_percent": 92,
    "filename": "backup.zip"
  }
}

Client Handling Rules

Clients should:

1. Check arqs_envelope.
2. Check arqs_type.
3. Decode body according to content_transfer_encoding.
4. Decompress according to content_encoding.
5. Decrypt according to encryption, if supported.
6. Dispatch by arqs_type.
7. Send requested receipts if supported and appropriate.
8. Treat transport ACK and client receipts as separate behaviors.

Clients should not:

assume the server interpreted headers
send receipts for receipts
put large payloads in headers
put secrets in outer headers of encrypted packets
retry invalid packets forever
auto-reply to presence.pong.v1

Server Handling Rule

The ARQS server should treat packet headers as opaque JSON metadata.

The server should not make delivery, routing, authorization, rate-limit, receipt, encryption, or application decisions based on these client-level header conventions unless a future server-side feature explicitly says otherwise.

For now:

headers are client convention, not server policy

## Forward Compatibility and Unknown Headers

Clients must ignore unknown header fields by default.

A convention-compliant v1 client should:

1. Require the v1 core fields it needs to decode the packet.
2. Use optional known fields when supported.
3. Ignore unknown fields.
4. Reject the packet only if a required field is missing, malformed, or declares an unsupported required behavior.

Custom/private extension headers should use a namespaced prefix, such as:

- `x-myapp-*`
- `vendor.example.*`
- `app.<name>.*`

Unknown extension headers must not change the meaning of required v1 fields.
