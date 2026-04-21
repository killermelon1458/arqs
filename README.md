# ARQS

**ARQS (Authenticated Relay Queue System)** is a small-scale, self-hosted text transport for scripts, lightweight clients, and homelab automation.

It exists to solve a very specific problem: getting messages, notifications, and lightweight commands between my own tools without having to re-solve networking every time. SMTP worked for some cases, but it is fundamentally awkward for script-to-script communication, depends on provider-specific setup, and tends to become one-way in practice. Bot-based approaches were also possible, but putting platform-specific messaging logic on every machine was the wrong direction. ARQS exists to centralize that work once and then reuse it everywhere.

ARQS is designed for **homelab and personal infrastructure use**, not as a public messaging platform. The focus is small-scale, self-hosted communication between scripts and purpose-built adapters. It is meant to be simple, practical, and robust enough to keep working in the kinds of environments homelabs actually have: unreliable power, imperfect networks, and machines that may go offline for a while.

## Why ARQS exists

ARQS was built to provide a reusable communication layer for things like:

- script notifications
- machine-to-machine messages
- lightweight command delivery
- adapters that bridge scripts into user-facing clients

The goal is not to make every script understand networking, retries, delivery state, identity, and routing on its own. The goal is to handle those concerns once in ARQS, then let other tools build on top of it.

## What ARQS is

At a high level, ARQS is an authenticated relay-and-queue service with an HTTP API. Clients register a node, create endpoints, explicitly link endpoints, send packets, poll for deliveries, and acknowledge deliveries after receipt or handling. The current client surface is built around node-centric authentication, endpoint-to-endpoint routing, explicit link-code-based linking, inbox polling, and transport ACK semantics.

ARQS uses a **store-and-forward** model:

1. a sender submits a packet to the server
2. the server queues a delivery for the destination
3. the recipient polls its inbox
4. the recipient acknowledges the delivery
5. the server deletes the queued delivery after ACK

The public client API exposes this directly through `/packets`, `/inbox`, and `/packet_ack`, with ACK explicitly described as transport-only and deleting the packet/delivery on the server. 

## Delivery model

ARQS is designed around a **polling + explicit acknowledgement** model rather than a push-only transport.

Recipients poll the server for pending deliveries. Both the current Discord adapter and the GUI client do this by long-polling the inbox and then acknowledging deliveries after they have been accepted locally or forwarded onward. 

This has a few practical consequences:

- ARQS is intended to tolerate temporary service interruptions better than direct live-only delivery.
- Queued deliveries remain on the server until they are acknowledged or expire.
- Delivery is **at least once**, not exactly once.
- Client code should be prepared to handle duplicates and use packet IDs for idempotency where needed.

The client already exposes packet IDs, delivery IDs, and ACK operations in a way that makes duplicate-aware handling practical. `send_packet()` can report `"accepted"` or `"duplicate"`, and deliveries carry a UUID packet ID. 

## Payload model

ARQS is a **text-oriented, payload-agnostic transport**. Packets carry structured fields including headers, body, data, and meta, and clients can use those however they want. The Python client models packets with `headers`, `body`, `data`, and `meta`, and the GUI and Discord adapter both treat the payload as application-defined content rather than something interpreted by ARQS itself. 

In practical terms, that means ARQS can be used for:

- human-readable messages
- notifications
- lightweight commands
- structured text payloads used by custom adapters or scripts

ARQS is not intended to be a general-purpose file transfer system or binary media platform.

## Scope

ARQS is built for **small-scale self-hosted use**.

The intended environment is:

- homelabs
- personal servers
- VPN-connected nodes
- local network services
- reverse-proxied HTTPS deployments in controlled environments

The design target is modest infrastructure rather than large public multi-tenant deployment. The long-term goal is for ARQS to become a stable foundational communication layer for my own tooling, with compatibility preserved where practical, while still allowing changes when they are genuinely necessary.

## Security and privacy model

ARQS supports authenticated access over HTTP or HTTPS, but HTTPS protects traffic in transit only. ARQS is not end-to-end encrypted by default, so the server is trusted enough to relay plaintext payloads, and anyone using a third-party host should assume the operator can read message contents.

If you expose ARQS beyond a trusted network, put it behind external controls you manage, such as a reverse proxy, TLS termination, firewall rules, or VPN access, rather than opening it directly to the public internet.

## Non-goals

ARQS is intentionally narrow in scope.

It is **not** trying to be:

- a public instant messaging platform
- a social/chat application
- a user discovery system
- a namespace or identity directory
- an enterprise-hardened internet service
- a replacement for reverse proxies, firewalls, or external security controls

ARQS is infrastructure for scripts and purpose-built clients, not a product for open user discovery or mass public communication.

## Current repository scope

This repository is focused on the core server and client-facing components.

At the time of writing, the project includes:

- the ARQS server
- a Python HTTP API client
- a desktop messages GUI
- a Discord DM adapter

The Python client exposes node registration, endpoint management, link request/redeem/revoke, packet sending, inbox polling, delivery acknowledgement, health, and stats operations. 

The included Discord adapter is currently **DM-only v1** and uses long-polling to bridge ARQS deliveries into Discord DMs. 

The GUI client provides a simple local messaging interface on top of the same transport model, including registration, endpoints, link codes, inbox polling, message sending, and local conversation history. 

## Project status

ARQS is **ready for real homelab use**, but it is still under active development.

The goal is long-term stability, especially around the API and packet schema, but compatibility is a goal rather than a guarantee at this stage. Where possible, changes should preserve older behavior or remain easy to adapt to. Where necessary, breaking changes may still happen while the project is maturing.

## High-level architecture

A typical ARQS flow looks like this:

```text
script/client
    -> send packet to ARQS server
    -> server queues delivery
    -> recipient polls inbox
    -> recipient handles packet
    -> recipient ACKs delivery
    -> server deletes queued delivery
```

At a high level, ARQS sits in the middle as a reusable transport layer: it handles authentication, routing, and queued delivery while clients and adapters decide what the payload means and how to present or act on it.

That separation is the main design goal. Scripts can stay simple, while richer clients and bridges can build on the same underlying message path.
