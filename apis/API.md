

# ARQS Python API Client

`arqs_api.py` is a thin synchronous Python client for the ARQS HTTP API.

It is intentionally small, standard-library only, and aligned to the server behavior that exists now. It is a transport client, not a full application framework.

## Design goals

* mirror the concrete server API closely
* avoid third-party runtime dependencies
* keep auth node-centric
* expose endpoint-to-endpoint transport directly
* preserve idempotent packet submission behavior through caller-provided `packet_id`
* support simple local identity persistence

## What this client covers

The client currently supports:

* node registration
* identity file save/load
* API key rotation
* identity deletion
* endpoint listing, creation, and deletion
* link-code request and redeem
* active-link listing and revoke
* packet send
* inbox polling
* ACK by `delivery_id` or `packet_id`
* health and stats calls

## Installation

No package install is required if you are using the single-file client directly.

It uses only the Python standard library.

## Quick start

```python
from arqs_api import ARQSClient

client = ARQSClient("http://127.0.0.1:8080")
identity = client.register(node_name="example-node")
identity.save("identity.json")

# Later:
client = ARQSClient.from_identity_file("http://127.0.0.1:8080", "identity.json")
```

## Authentication model

ARQS uses one API key per node.

By default, the client sends it in:

```text
X-ARQS-API-Key: <node-api-key>
```

The current server also accepts:

```text
Authorization: Bearer <node-api-key>
```

You can choose between those two server-supported auth modes by constructing the client with the default `api_key_header="X-ARQS-API-Key"` or with `api_key_header="Authorization"`. Arbitrary custom header names are not supported by the current server auth path.

## Core types

The client exposes small dataclasses for the main transport objects:

* `NodeIdentity`
* `RotatedKey`
* `IdentityDeleteResult`
* `Endpoint`
* `LinkCode`
* `Link`
* `PacketSendResult`
* `DeliveryPacket`
* `Delivery`
* `HealthStatus`
* `ServerStats`

`HealthStatus` now contains only `status` and `time`.

`ServerStats` contains aggregate counters plus `time`.

## Error model

The client raises:

* `ARQSError` for general client-side usage issues
* `ARQSHTTPError` for non-2xx server responses
* `ARQSConnectionError` for DNS/TCP/timeout/transport failures

`ARQSHTTPError` includes:

* `status_code`
* `detail`
* parsed response JSON when available
* raw response text when available

## Identity management

### Register

```python
identity = client.register(node_name="worker-1")
```

Returns a `NodeIdentity` and, by default, adopts it into the client instance.

### Save identity

```python
identity.save("identity.json")
# or
client.save_identity("identity.json")
```

### Load identity

```python
client = ARQSClient.from_identity_file(base_url, "identity.json")
```

### Rotate key

```python
rotated = client.rotate_key()
```

### Delete identity

```python
result = client.delete_identity()
```

This is destructive and clears the adopted identity from the client by default.

## Endpoint management

### List endpoints

```python
endpoints = client.list_endpoints()
```

### Create endpoint

```python
endpoint = client.create_endpoint(
    endpoint_name="notifications",
    kind="message",
    meta={"scope": "ops"},
)
```

### Delete endpoint

```python
client.delete_endpoint(endpoint.endpoint_id)
```

The server may reject deletion if the endpoint is default, still linked, or still has queued deliveries.

## Link flow

### Request link code

```python
link_code = client.request_link_code(
    source_endpoint_id=identity.default_endpoint_id,
    requested_mode="bidirectional",
)
```

### Redeem link code

```python
link = client.redeem_link_code(code=link_code.code, destination_endpoint_id=some_endpoint_id)
```

### List active links

```python
links = client.list_links()
```

### Revoke link

```python
client.revoke_link(link.link_id)
```

## Sending packets

### Minimal send

```python
result = client.send_packet(
    from_endpoint_id=source_endpoint_id,
    to_endpoint_id=dest_endpoint_id,
    body="hello",
)
```

### Structured payload send

```python
result = client.send_packet(
    from_endpoint_id=source_endpoint_id,
    to_endpoint_id=dest_endpoint_id,
    data={"event": "deploy_finished", "ok": True},
    headers={"content_type": "application/json"},
    meta={"source": "buildbot"},
)
```

### Notes

* At least one of `body` or non-empty `data` is required.
* The client generates a random `packet_id` if you do not provide one.
* You may provide your own `packet_id` for retries and idempotency.
* A duplicate resend of the exact same packet returns `result="duplicate"`.

## Polling inbox

```python
deliveries = client.poll_inbox(wait=20, limit=100)
```

This polls the node inbox, not an individual endpoint inbox.

Returned deliveries include the destination endpoint in the delivery object and both source and destination endpoint ids in the packet.

## ACK

### ACK by delivery id

```python
client.ack_delivery(delivery_id, status="handled")
```

### ACK by packet id

```python
client.ack_packet(packet_id, status="handled")
```

The server deletes the delivery and packet on ACK.

## Health and stats

```python
health = client.health()
stats = client.stats()
```

`health()` expects the minimal public `/health` payload: `status` and `time`.

`stats()` expects aggregate totals plus `time`.

Depending on server `[observability]` config, `stats()` may raise `ARQSHTTPError` with `401`, `403`, or `404` instead of returning data.

## Example end-to-end script

```python
from arqs_api import ARQSClient

base_url = "http://127.0.0.1:8080"

alice = ARQSClient(base_url)
alice_identity = alice.register("alice")

bob = ARQSClient(base_url)
bob_identity = bob.register("bob")

code = alice.request_link_code(alice_identity.default_endpoint_id)
link = bob.redeem_link_code(code.code, bob_identity.default_endpoint_id)

send_result = alice.send_packet(
    from_endpoint_id=alice_identity.default_endpoint_id,
    to_endpoint_id=bob_identity.default_endpoint_id,
    body="hello from alice",
)

for delivery in bob.poll_inbox(wait=0, limit=10):
    print(delivery.packet.body)
    bob.ack_delivery(delivery.delivery_id, status="handled")
```

## Limitations

This client is intentionally thin.

It does **not** provide:

* async I/O
* websocket transport
* local conversation storage
* contact management abstractions
* automatic retries
* duplicate suppression beyond server-side packet-id semantics
* encryption, signing, or payload schema enforcement

## Intended use

This file is a good fit for:

* scripts
* service integrations
* test harnesses
* proof-of-concept clients
* reference behavior for ports to other languages

If you are building a larger application, treat this client as a low-level transport layer and add your own identity storage, retry logic, schema validation, and UI rules above it.

## Development status

This client should currently be treated as **experimental**.

It is aligned to the current ARQS server implementation, but the surrounding system is still under active validation and local testing. Until that testing is complete, this client should be treated as a development and integration tool rather than a stable long-term interface guarantee.

## Security status

This client does not change the security posture of the server.

Important realities right now:

* the current server stores packets in plain text
* end-to-end encryption is not implemented here
* security testing has so far been limited
* no third-party penetration testing or outside security audit has been completed

So while the client supports authenticated API access, it should not be represented as part of a hardened or audited secure messaging stack.
