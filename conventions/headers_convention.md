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
blob.manifest.v1
blob.chunk.v1
blob.receipt.v1
encrypted.v1
command.v1
command.response.v1
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

"receipt_request": ["received", "processed", "read"]

Supported values:

received  = receiver client obtained and durably stored the packet
processed = receiver client successfully handled the packet
read      = a human user viewed/read the message

Rules:

Receipts must not request receipts.
If arqs_type starts with receipt., clients should ignore receipt_request.
Unsupported receipt types may be ignored.

correlation_id

A UUID used to group related packets together.

Useful for:

command/response chains
file transfers
multi-packet workflows
logs/tracing
conversation threads

Example:

"correlation_id": "9b777b0e-6d7a-40b1-9c39-885d7bbd76a1"

causation_id

A UUID identifying the packet/event that caused this packet.

Useful for:

reply tracking
receipt tracking
command responses
workflow tracing

Example:

"causation_id": "uuid-of-original-packet"

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
    "encryption": "none"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "received_at": "2026-04-24T13:00:00Z"
  },
  "meta": {
    "client": "arqs_appkit"
  }
}

Processed Receipt

{
  "headers": {
    "arqs_envelope": "v1",
    "arqs_type": "receipt.processed.v1",
    "content_type": "application/json",
    "content_transfer_encoding": "utf-8",
    "content_encoding": "identity",
    "encryption": "none"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "processed_at": "2026-04-24T13:01:00Z",
    "status": "success"
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
    "encryption": "none"
  },
  "body": null,
  "data": {
    "receipt_id": "uuid",
    "for_packet_id": "uuid-of-original-packet",
    "read_at": "2026-04-24T13:05:00Z"
  },
  "meta": {
    "client": "arqs_messages_gui"
  }
}

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

Clients should not:

assume the server interpreted headers
send receipts for receipts
put large payloads in headers
put secrets in outer headers of encrypted packets
retry invalid packets forever

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
