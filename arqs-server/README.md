# `arqs-server/README.md`

# ARQS Server

ARQS is an authenticated, explicit-link, store-and-forward packet relay.

This server is intentionally narrow in scope. It owns node identity, endpoint ownership, explicit link creation, packet acceptance, queued delivery, and transport acknowledgements. It does **not** try to be a chat platform, namespace directory, searchable archive, or message-semantics engine.

## Current scope

The current server implements:

* node registration with one API key per node
* automatic creation of a default endpoint at registration time
* authenticated endpoint management for owned endpoints
* explicit link-code request and redeem flow
* active-link listing and revocation
* endpoint-to-endpoint packet submission
* node-scope inbox polling with long-poll support
* transport ACK that deletes the queued delivery and packet
* health and queue/statistics endpoints
* SQLite-backed persistence with configurable retention and limits

## What ARQS is not

ARQS does **not** currently provide:

* global user discovery
* open addressing
* profile or contact search
* conversations or threads as a server primitive
* read receipts or human-read semantics
* searchable message history
* payload encryption semantics
* client UX conventions
* adapter provisioning workflow in the server runtime described here

## Transport model

ARQS routes traffic **endpoint to endpoint**.

A node authenticates with its API key and may only act on endpoints it owns. A packet is accepted only when there is an active directed route from `from_endpoint_id` to `to_endpoint_id`. User-facing links are created through explicit link codes and may be bidirectional or directional depending on the requested mode.

Delivery is **at least once**. If a delivery is polled but not ACKed, clients must tolerate seeing the same `packet_id` again.

ACK is **transport-only**. It means the destination node acknowledged handling of the delivery at the transport layer. It does not mean a human read the payload.

## Server behavior summary

### Registration

`POST /register` creates:

* a new node
* a new API key for that node
* one default endpoint owned by that node

The API key is only returned at registration time or during key rotation. Store it securely.

### Identity lifecycle

A node can:

* rotate its own API key with `POST /identity/rotate-key`
* delete its own identity and associated server-side records with `DELETE /identity`

Identity deletion is destructive. It removes the node and cascades through owned endpoints and related transport state.

### Endpoints

A node may own multiple endpoints. The default endpoint cannot be deleted. Non-default endpoints can be created and deleted, but deletion is blocked if the endpoint is still actively linked or still has queued deliveries.

### Links

Links are created through a two-step explicit-link flow:

1. One node requests a short-lived link code for one of its endpoints.
2. Another node redeems that code into one of its own endpoints.

On success, the server creates:

* an active link record
* one or two directed route permissions, depending on link mode

Revoking a link also revokes the directed routes created from that link.

### Packets

Packets are versioned transport objects submitted with a caller-supplied `packet_id`. The current implementation accepts `version = 1` only.

A packet must include at least one of:

* `body`
* non-empty `data`

If the same `packet_id` is submitted again with identical content, ARQS returns a duplicate result instead of creating a second queued delivery. If the same `packet_id` is reused with different content, ARQS rejects it.

### Inbox polling and ACK

Inbox polling is done at **node scope**, not endpoint scope. The destination node receives deliveries for its owned endpoints.

Polling can long-poll up to the configured server maximum. Returned deliveries are marked as delivered. A later `POST /packet_ack` removes the delivery and its packet from the server.

## Security model

Current security properties:

* authenticated requests use the configured API key header
* nodes may only manage their own endpoints
* packets require an active directed route
* explicit linking is required before delivery is allowed
* cache-control is forced to `no-store`
* optional blacklist and proxy/header trust controls exist in config

What this does **not** imply:

* payload confidentiality from the server itself
* end-to-end encryption
* content validation beyond transport constraints
* abuse resistance beyond the configured limits, auth, and deployment choices

## Configuration

The server reads config from `ARQS_CONFIG` or `/app/config.toml` by default.

Major configuration areas:

* `server`: bind host, port, app name, API key header
* `storage`: database path and WAL behavior
* `retention`: default packet TTL, link-code TTL, optional no-expiry mode
* `limits`: max packet size, queue limits, batch size, long-poll cap
* `rate_limit`: send-rate window and maximum sends per window
* `network`: trusted proxies and trusted forwarded headers
* `blacklist`: blocked client IPs and blocked node IDs

## Docker

The repository includes Docker support and a compose file. In the shipped config, the server stores its SQLite database under `/data/arqs.db`, so the `/data` path should be persisted.

Typical deployment shape:

* mount a persistent volume to `/data`
* mount a config file into the container
* expose the HTTP port only as intended by your network model
* put TLS / reverse proxy / Cloudflare handling in front of it if internet-facing

## Operational notes

ARQS is best treated as a transport core, not an end-user product by itself.

For production use, you should assume the following are the caller’s responsibility:

* secure storage of node API keys
* packet idempotency handling
* payload schema validation
* local identity persistence
* higher-level UX such as contacts, chat labeling, or retry policy
* HTTPS termination and edge protection

## API summary

Authentication header:

```text
X-ARQS-API-Key: <node-api-key>
```

Implemented endpoints:

* `POST /register`
* `POST /identity/rotate-key`
* `DELETE /identity`
* `GET /endpoints`
* `POST /endpoints`
* `DELETE /endpoints/{endpoint_id}`
* `POST /links/request`
* `POST /links/redeem`
* `GET /links`
* `DELETE /links/{link_id}`
* `POST /packets`
* `GET /inbox`
* `POST /packet_ack`
* `GET /health`
* `GET /stats`

---

## Minimal example flow

1. Register node A.
2. Register node B.
3. Node A requests a link code for one of its endpoints.
4. Node B redeems that code into one of its endpoints.
5. Node A sends a packet from its endpoint to B’s endpoint.
6. Node B polls `/inbox`.
7. Node B ACKs the delivery.

That is the core ARQS transport loop.

---

## Development status

ARQS is currently **experimental**.

It is under active development and the transport core is still being validated against the intended design. Until the planned local testing and validation work is finished, this server should be treated as a development system rather than a production-ready messaging backend.

That means:

* interfaces may still change
* behavior may still be tightened or corrected
* operational assumptions may still be revised
* compatibility should not be assumed across early revisions unless explicitly documented

## Security status

The current system should **not** be described as hardened.

Important current limitations:

* packets are currently stored on the server in plain text
* end-to-end encryption is not implemented in the server transport described here
* formal in-house penetration testing is still minimal
* third-party security review and third-party penetration testing have not been done

So the current security posture is best described as:

* authenticated transport core
* explicit-link routing controls
* basic server-side limits and access checks
* **not yet security-audited**
* **not yet hardened for strong adversarial use**

Anyone deploying ARQS should assume that the present implementation is suitable for development, controlled testing, and architecture iteration, not for high-trust or high-risk production use.

## Project status

This README describes the **current server implementation** and its concrete transport surface.

Client UX, adapters, bootstrap flows, and higher-level application behavior may evolve independently. They should not be assumed from this server README unless they are explicitly documented in the relevant component.

