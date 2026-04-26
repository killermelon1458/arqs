# ARQS Discord Bot v1 Implementation Plan

## Purpose

This document defines the v1 implementation plan for a new AppKit-backed ARQS Discord bot.

The bot is a DM-only Discord bridge for ARQS. It allows Discord users to link ARQS contacts, send and receive ARQS messages, receive ARQS notifications, send simple ARQS command packets, and optionally send ARQS client-level receipts when Discord delivery or user read actions occur.

The bot should be rebuilt from scratch around AppKit instead of duplicating ARQS identity, transport, packet convention, retry, and client setup logic inside the Discord adapter.

---

## High-Level Goals

The v1 bot must support:

* Discord DM-only operation
* AppKit-managed ARQS runtime setup
* AppKit-managed ARQS identity/config/transport/outbox behavior
* one hidden ARQS endpoint per Discord user/contact binding
* link-code request and redeem flows
* bidirectional, send-only, and receive-only ARQS links
* per-Discord-user active contact selection
* reply-based routing back to the correct ARQS contact
* outbound Discord DM text as `message.v1`
* inbound ARQS packets forwarded to Discord DMs
* inbound `notification.v1` rendered clearly for Discord users
* `/command` for sending fire-and-forget `command.v1` packets
* optional Discord-delivered receipts after the bot successfully DMs a user
* optional reaction-based read receipts when the user reacts to a forwarded bot message
* queued/background sending through AppKit when configured
* ACK after successful Discord delivery for inbound packets
* local Discord bridge state persistence

---

## Non-Goals for v1

Do not implement these in v1:

* real command-response waiting
* arqs command execution inside the Discord bot
* Discord server/guild channel bridging
* Discord attachments or file transfer
* blob transfer over ARQS
* encryption
* presence ping/pong automation
* mobile push integration
* migration from old Discord adapter state
* rich embed UI for every packet type
* full plugin framework
* automatic Discord read detection without user action

---

## Main Files

Create:

```text
arqs_discord_appkit_bot.py
```

Create:

```text
ARQS_Discord_AppKit_Bot_README.md
```

---

## Dependency Model

The bot depends on:

* `discord.py`
* `python-dotenv`
* existing ARQS AppKit package
* existing `arqs_api.py` through AppKit
* existing `arqs_conventions.py` through AppKit and direct rendering helpers when needed

The bot should import AppKit as the package currently exists in the repo/project files.

Preferred import:

```python
from appkit import ARQSApp
```

If the package is renamed later, update imports consistently.

The bot should not reimplement raw ARQS HTTP calls when AppKit already exposes the needed behavior.

---

## Layer Boundaries

### Discord Bot Owns

The Discord bot owns Discord-specific behavior:

* Discord login/session lifecycle
* slash commands
* DM-only validation
* Discord user IDs
* Discord user contact bindings
* active contact per Discord user
* reply-to-contact routing
* reaction-to-read-receipt behavior
* Discord message splitting
* Discord message forwarding
* Discord button/confirmation UX
* Discord-specific bridge state

### AppKit Owns

AppKit owns ARQS runtime behavior:

* ARQS config loading
* HTTP/HTTPS transport policy
* identity loading/registration
* endpoint/client setup
* outbox/queued send behavior
* retry behavior
* packet convention construction through `send_type()`
* raw `ARQSClient` access through AppKit when needed

### `arqs_api.py` Owns

The raw API client remains low-level transport only.

The Discord bot should not add app behavior to `arqs_api.py`.

### `arqs_conventions.py` Owns

The convention helper remains the packet/header/body interpretation layer.

The Discord bot may use convention helpers for rendering and packet type checks, but the bot must not redefine the convention.

---

## Runtime Config

The bot uses one AppKit app name:

```text
discord-adapter
```

Default state directory:

```text
~/.arqs/discord-adapter/
```

Files:

```text
~/.arqs/discord-adapter/
  config.json
  identity.json
  contacts.json
  outbox.sqlite3
  inbox.sqlite3
  discord_state.json
  appkit.log
```

### `config.json`

Example:

```json
{
  "app_name": "discord-adapter",
  "base_url": "https://arqs.example.com",
  "node_name": "discord-adapter",
  "default_endpoint_name": "discord-control",
  "default_endpoint_kind": "discord_control",
  "transport_policy": "prefer_https",
  "allow_local_http_auth": true,
  "allow_public_http_auth": false,
  "delivery_mode": "queued",
  "retry_policy": "until_expired",
  "max_attempts": 20,
  "expires_after_seconds": 86400,
  "poll_wait_seconds": 20,
  "poll_limit": 100,
  "discord_sync_commands_on_start": false,
  "discord_log_level": "INFO",
  "receipt_default_mode": "off"
}
```

The Discord token must come from the environment:

```text
DISCORD_BOT_TOKEN=...
```

Do not store the Discord token in `config.json`.

---

## HTTP/HTTPS Policy

HTTP/HTTPS behavior is AppKit-config driven.

The Discord bot must not implement an independent transport decision tree.

The bot passes the config to AppKit and lets AppKit enforce:

```text
transport_policy
allow_local_http_auth
allow_public_http_auth
transport_preferences
```

Supported `transport_policy` values:

```text
require_https
prefer_https
allow_http
```

Expected behavior:

```text
require_https
  HTTPS is required. Startup fails if HTTPS is not usable.

prefer_https
  HTTPS is used when available.
  Local/private HTTP may be allowed if allow_local_http_auth=true.
  Public HTTP is blocked unless allow_public_http_auth=true.

allow_http
  HTTP is allowed intentionally.
```

---

## Discord State

The bot stores Discord-specific state in:

```text
discord_state.json
```

### Top-Level Shape

```json
{
  "bindings": [],
  "pending_links": [],
  "active_contacts": {},
  "seen_deliveries": [],
  "reply_index": {},
  "receipt_settings": {},
  "receipt_index": {}
}
```

---

## Binding Model

A binding connects one Discord user to one ARQS contact over one hidden local ARQS endpoint.

### Binding Shape

```json
{
  "binding_id": "uuid",
  "discord_user_id": "123456789",
  "local_endpoint_id": "uuid",
  "remote_endpoint_id": "uuid",
  "link_id": "uuid",
  "label": "Contact 1",
  "link_mode": "bidirectional",
  "can_send": true,
  "can_receive": true,
  "status": "active",
  "created_at": "2026-04-25T13:00:00Z",
  "updated_at": "2026-04-25T13:00:00Z"
}
```

### Required Fields

* `binding_id`: local UUID
* `discord_user_id`: Discord user ID string
* `local_endpoint_id`: bot-owned hidden ARQS endpoint
* `remote_endpoint_id`: linked remote ARQS endpoint
* `link_id`: ARQS server link ID
* `label`: user-facing contact label
* `link_mode`: ARQS link mode
* `can_send`: whether Discord may send to this contact
* `can_receive`: whether Discord may receive from this contact
* `status`: active/severed/unknown
* `created_at`
* `updated_at`

---

## Pending Link Model

Pending links are link codes created by the bot and waiting for the remote side to redeem.

### Pending Link Shape

```json
{
  "pending_id": "uuid",
  "discord_user_id": "123456789",
  "local_endpoint_id": "uuid",
  "code": "ABC123",
  "requested_mode": "bidirectional",
  "label": "Contact 1",
  "created_at": "2026-04-25T13:00:00Z",
  "expires_at": "2026-04-25T13:15:00Z"
}
```

Expired pending links should be pruned during reconciliation.

---

## Active Contact Model

`active_contacts` maps Discord user IDs to binding IDs.

Example:

```json
{
  "active_contacts": {
    "123456789": "binding-uuid"
  }
}
```

Rules:

* If a user has one active binding, use it automatically.
* If a user has multiple active bindings and one active contact, use the active contact.
* If a user replies to a forwarded bot message, route to the contact associated with that forwarded message.
* If a user has multiple active bindings and no active contact, require `/use_contact`.

---

## Reply Index

`reply_index` maps Discord bot message IDs to the binding that produced them.

Example:

```json
{
  "reply_index": {
    "discord_message_id": {
      "discord_user_id": "123456789",
      "binding_id": "binding-uuid",
      "packet_id": "original-arqs-packet-id"
    }
  }
}
```

When a Discord user replies to a forwarded ARQS message, the bot uses this index to choose the correct ARQS contact.

---

## Receipt Settings

Receipts are user-scoped and optionally contact-scoped.

### Receipt Settings Shape

```json
{
  "receipt_settings": {
    "123456789": {
      "default_mode": "off",
      "contacts": {
        "binding-id-1": "discord_delivered",
        "binding-id-2": "reaction_read"
      }
    }
  }
}
```

### Supported Receipt Modes

```text
off
  Send no client-level ARQS receipt packets.

discord_delivered
  Send receipt.received.v1 after the bot successfully sends the message to the Discord user.

reaction_read
  Send receipt.read.v1 when the Discord user reacts to the forwarded bot message.

discord_delivered_and_reaction_read
  Send receipt.received.v1 after Discord delivery and receipt.read.v1 after user reaction.
```

### Receipt Meaning

`discord_delivered` means:

```text
The Discord bot successfully sent the forwarded message into the user's DM channel.
```

It does not mean the user read the message.

`reaction_read` means:

```text
The Discord user intentionally reacted to the forwarded bot message, and the bot treats that reaction as a manual read acknowledgement.
```

---

## Receipt Index

`receipt_index` maps forwarded Discord bot messages to original ARQS packets for reaction-based read receipts.

Example:

```json
{
  "receipt_index": {
    "discord_message_id": {
      "discord_user_id": "123456789",
      "binding_id": "binding-uuid",
      "original_packet_id": "uuid",
      "original_from_endpoint_id": "uuid",
      "original_to_endpoint_id": "uuid",
      "original_correlation_id": "uuid-or-null",
      "read_receipt_sent": false,
      "delivered_receipt_sent": true,
      "created_at": "2026-04-25T13:00:00Z"
    }
  }
}
```

This index is required because Discord reaction events identify the Discord message, not the ARQS packet.

---

## One-Way Link Support

The bot must directly support directional ARQS links.

User-facing link modes:

```text
bidirectional
send_only
receive_only
```

ARQS link modes:

```text
bidirectional
a_to_b
b_to_a
```

When the bot requests a link code, its local endpoint is endpoint A.

Mapping for `/request_link_code`:

```text
bidirectional -> bidirectional
send_only     -> a_to_b
receive_only  -> b_to_a
```

When the bot redeems a link code, direction must be calculated from the returned link object.

### Direction Calculation

Given:

```text
endpoint_a_id
endpoint_b_id
mode
local_endpoint_id
```

Rules:

```text
if mode == bidirectional:
  can_send = true
  can_receive = true

if mode == a_to_b:
  can_send = local_endpoint_id == endpoint_a_id
  can_receive = local_endpoint_id == endpoint_b_id

if mode == b_to_a:
  can_send = local_endpoint_id == endpoint_b_id
  can_receive = local_endpoint_id == endpoint_a_id
```

Store `can_send` and `can_receive` directly on each binding.

---

## Sending Rules for One-Way Links

Before sending a normal message or `/command`, check:

```text
binding.can_send == true
```

If false, reject the send with a user-facing message:

```text
This contact is receive-only from Discord. You can receive messages from it, but you cannot send messages back over this ARQS link.
```

Do not queue messages that are known to be impossible because of link direction.

---

## Receiving Rules for One-Way Links

If the ARQS server delivers a packet to the bot, the bot should normally forward it to Discord even if local cached `can_receive` says false.

Behavior:

```text
if binding exists and can_receive=false:
  log a warning
  forward the packet anyway
  refresh/reconcile link state
  ACK after Discord send succeeds
```

Reason:

```text
The server delivery is stronger evidence than stale local direction cache.
```

---

## Receipt Rules for One-Way Links

Receipts require a usable reverse send route.

Rules:

```text
if receipt mode is off:
  send no receipt

if receipt mode requires sending a receipt and binding.can_send=false:
  do not send the receipt
  do not add reaction-based read receipt behavior
  optionally log: receipts unavailable because no reverse route exists

if binding.can_send=true:
  receipt packets may be sent according to the user's receipt setting
```

For receive-only contacts, the bot may receive messages but cannot send receipts back.

---

## Slash Commands

The bot should register these slash commands:

```text
/request_link_code mode
/redeem_link_code code
/links
/use_contact contact
/current_contact
/rename_contact new_name
/delete_link
/command text contact(optional)
/receipts status
/receipts off contact(optional)
/receipts discord_delivered contact(optional)
/receipts reaction_read contact(optional)
/receipts discord_delivered_and_reaction_read contact(optional)
/status
/flush_outbox
```

All commands are DM-only unless explicitly stated otherwise.

---

## `/request_link_code`

### Purpose

Create a hidden ARQS endpoint for the Discord user and request a link code for it.

### Parameters

```text
mode: bidirectional | send_only | receive_only
```

Default:

```text
bidirectional
```

### Flow

1. Validate command is used in DM.
2. Resolve Discord user ID.
3. Create hidden ARQS endpoint:

```text
endpoint_name = discord:dm:<discord_user_id>:<short_uuid>
kind = discord_dm
meta = {"discord_user_id": "...", "scope": "dm"}
```

4. Convert user-facing mode to ARQS link mode.
5. Call ARQS link-code request through AppKit's client.
6. Save pending link in `discord_state.json`.
7. Reply to the user with:

```text
Link code
Mode
Expiration time
Contact label
```

### Response Example

```text
Link code created for Contact 1.

Code: ABC123
Mode: bidirectional
Expires in 15 minutes at 1:45 PM CDT

Share this code with the other ARQS user. When the link becomes active, I will DM you.
```

---

## `/redeem_link_code`

### Purpose

Redeem another ARQS user's link code and create a Discord contact binding.

### Parameters

```text
code: string
```

### Flow

1. Validate command is used in DM.
2. Create hidden endpoint for Discord user.
3. Redeem link code through AppKit's client.
4. Determine remote endpoint.
5. Calculate `can_send` and `can_receive`.
6. Create binding.
7. Set active contact if needed.
8. Save state.
9. Reply with contact label and direction.

### Response Example

```text
Link redeemed successfully as Contact 1.
Direction: bidirectional
Can send: yes
Can receive: yes
```

---

## `/links`

### Purpose

List a Discord user's linked ARQS contacts.

### Output Example

```text
Linked contacts:
* 1. server-agent — bidirectional
  2. monitor — receive-only
  3. command-target — send-only

* = active contact
```

Show direction using user-facing names:

```text
bidirectional
send-only
receive-only
```

---

## `/use_contact`

### Purpose

Select active contact for normal outbound Discord DMs and `/command` packets.

### Parameters

```text
contact: label or number from /links
```

### Behavior

* Match by number first if numeric.
* Match exact label next.
* Allow prefix label match if unambiguous.
* Save active binding for that Discord user.

---

## `/current_contact`

### Purpose

Show current active contact and capabilities.

### Output Example

```text
Current active contact: server-agent
Direction: bidirectional
Can send: yes
Can receive: yes
Receipts: reaction_read
```

If no active contact is selected:

```text
No active contact is selected.
```

---

## `/rename_contact`

### Purpose

Rename the active contact.

### Parameters

```text
new_name: string
```

### Rules

* New name cannot be empty.
* New name must be unique for that Discord user.
* Rename only affects local Discord bot state.
* Rename does not change ARQS server endpoint names.

---

## `/delete_link`

### Purpose

Delete the active contact link.

### Behavior

Use a Discord confirmation view with:

```text
Cancel
Delete link
```

On confirm:

1. Revoke server-side link if possible.
2. Attempt to delete hidden local endpoint if only active link.
3. Remove binding from local state.
4. Remove reply-index entries for that binding.
5. Remove receipt-index entries for that binding.
6. Update active contact.
7. Save state.
8. Report success or partial failure.

### Warning Text

```text
This will delete the active ARQS link for this contact. This cannot be undone. Messages will stop until you create a new link.
```

---

## `/command`

### Purpose

Send a fire-and-forget `command.v1` packet over ARQS.

This does not wait for a `command.response.v1` in v1.

### Parameters

```text
text: full command text
contact: optional contact label/number
```

If `contact` is omitted, use the same active/reply/default contact rules as normal messages.

### Link Direction Rule

Require:

```text
binding.can_send == true
```

If false, reject the command.

### Packet Type

```text
command.v1
```

### Packet Shape

Headers:

```json
{
  "arqs_envelope": "v1",
  "arqs_type": "command.v1",
  "content_type": "application/json",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none",
  "correlation_id": "uuid"
}
```

Body:

```text
<raw command text>
```

Data:

```json
{
  "command_id": "uuid",
  "command": "<raw command text>",
  "args": {
    "raw": "<raw command text>"
  },
  "created_at": "2026-04-25T13:00:00Z"
}
```

Meta:

```json
{
  "client": "appkit/discord-adapter",
  "adapter": "discord_dm",
  "discord_user_id": "123456789",
  "discord_user": "name#0000",
  "discord_interaction_id": "..."
}
```

### User Response

```text
Command queued for server-agent.
```

or:

```text
Command sent to server-agent.
```

depending on AppKit send result.

---

## `/receipts`

### Purpose

Allow each Discord user to control ARQS client-level receipts.

Receipt settings may be default-per-user or contact-specific.

### Subcommands

```text
/receipts status
/receipts off contact(optional)
/receipts discord_delivered contact(optional)
/receipts reaction_read contact(optional)
/receipts discord_delivered_and_reaction_read contact(optional)
```

### Behavior

No contact argument:

```text
Set this Discord user's default receipt mode.
```

With contact argument:

```text
Set receipt mode for that specific binding/contact.
```

### `/receipts status` Output Example

```text
Receipt settings:
Default: reaction_read

Contact overrides:
1. server-agent — discord_delivered_and_reaction_read
2. monitor — off
```

### Capability Warning

If the selected contact is receive-only from Discord and cannot send back, show:

```text
Receipts are configured, but this contact has no reverse send route. Receipts will not be sent unless the link direction changes.
```

---

## `/status`

### Purpose

Show bot/AppKit status.

Suggested output:

```text
ARQS Discord Bot Status
App: discord-adapter
ARQS base URL: https://arqs.example.com
Node loaded: yes
Bindings: 3
Pending links: 1
Poll wait: 20 seconds
Outbox: enabled
```

Do not reveal API keys.

---

## `/flush_outbox`

### Purpose

Manually flush queued AppKit outbound messages.

Behavior:

* DM-only.
* Calls AppKit outbox flush if available.
* Reports sent/failed/dead-letter counts.
* Does not expose sensitive packet details.

---

## Normal DM Message Handling

When a Discord user sends a normal DM to the bot:

1. Ignore bot messages.
2. Reject guild/server messages.
3. Ignore empty messages.
4. Resolve outbound binding:

   * reply target if replying to a forwarded bot message
   * only contact if one exists
   * active contact if selected
   * otherwise require `/use_contact`
5. Check `binding.can_send`.
6. Send ARQS `message.v1` through AppKit.
7. Update binding timestamp.
8. Set active contact to the used binding.
9. Save state.
10. Add ✅ reaction if possible.

### Outbound Packet Type

```text
message.v1
```

### Outbound Headers

```json
{
  "arqs_envelope": "v1",
  "arqs_type": "message.v1",
  "content_type": "text/plain; charset=utf-8",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none"
}
```

### Outbound Meta

```json
{
  "client": "appkit/discord-adapter",
  "adapter": "discord_dm",
  "discord_user_id": "123456789",
  "discord_user": "name#0000",
  "discord_message_id": "...",
  "discord_reply_to_message_id": "... optional ..."
}
```

---

## Inbound ARQS Polling

The bot should run an async polling task after Discord `on_ready`.

Use AppKit-managed client access inside `asyncio.to_thread()`.

Flow:

```text
while bot is running:
  poll ARQS inbox using configured wait/limit
  for each delivery:
    handle delivery
```

Suggested poll call:

```python
deliveries = await asyncio.to_thread(
    app.require_client().poll_inbox,
    wait=poll_wait_seconds,
    limit=poll_limit,
    request_timeout=poll_wait_seconds + 10,
)
```

---

## Inbound Delivery Handling

For each delivery:

1. Read delivery ID and packet.
2. If delivery already in `seen_deliveries`, ACK and skip.
3. Resolve binding by `packet.to_endpoint_id`.
4. If no binding exists:

   * log warning
   * ACK to avoid endless redelivery
   * skip
5. Render packet for Discord.
6. Split rendered text into Discord-safe chunks.
7. Send chunks to Discord user.
8. Store reply-index entries for sent Discord messages.
9. Store receipt-index entries when receipts are enabled and possible.
10. Send automatic Discord-delivered receipt if enabled and possible.
11. Set active contact for that Discord user.
12. Save state.
13. ACK ARQS delivery after successful Discord send.

ACK must happen after the Discord send succeeds.

If Discord forwarding fails, do not ACK. The ARQS server can redeliver later.

---

## Rendering Inbound Packets

### `message.v1`

Format:

```text
[Contact Label] message text
```

### `notification.v1`

Format:

```text
[Contact Label] 🔔 warning: Disk space low
/mnt/docker is at 92%
```

Fields to prefer from `data`:

```text
level
title
body
source
host
script
tags
```

Fallback to rendered body/data text.

### `command.v1`

Format:

```text
[Contact Label] command.v1
<command body>
```

### `command.response.v1`

Format:

```text
[Contact Label] command.response.v1
<result or error>
```

### Unknown Type

Format:

```text
[Contact Label] <arqs_type or packet>
<rendered text/json>
```

Use shared convention rendering as fallback.

---

## Discord Message Splitting

Discord messages should be split below the hard 2000-character limit.

Use a safe chunk limit:

```text
1900 characters
```

Splitting strategy:

1. If content <= 1900, send as one message.
2. Prefer splitting at newline.
3. Otherwise split at space.
4. Otherwise hard split.

---

## Discord-Delivered Receipts

When a forwarded ARQS packet is successfully sent to a Discord user's DM channel and receipt mode includes `discord_delivered`, send an ARQS receipt packet.

Receipt type:

```text
receipt.received.v1
```

Receipt route:

```text
from_endpoint_id = original packet.to_endpoint_id
to_endpoint_id   = original packet.from_endpoint_id
```

Only send if:

```text
binding.can_send == true
```

Receipt data:

```json
{
  "receipt_id": "uuid",
  "for_packet_id": "original-packet-id",
  "receipt_type": "discord_delivered",
  "status": "ok",
  "delivered_to_discord_at": "2026-04-25T13:00:00Z",
  "discord_user_id": "123456789"
}
```

Headers:

```json
{
  "arqs_envelope": "v1",
  "arqs_type": "receipt.received.v1",
  "content_type": "application/json",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none",
  "correlation_id": "original-correlation-id-if-present",
  "causation_id": "original-packet-id"
}
```

If sending the receipt fails, queue it if AppKit delivery mode supports queueing. Otherwise log the failure.

---

## Reaction-Based Read Receipts

When receipt mode includes `reaction_read`, the bot should allow a Discord user to send a read receipt by reacting to a forwarded bot message.

### Supported Reaction

Use one configured reaction emoji:

```text
✅
```

Alternative later:

```text
👀
```

The v1 default should be ✅.

### Forwarded Message Setup

For messages eligible for read receipts:

1. Send forwarded Discord message.
2. Add the configured read-receipt reaction emoji to the bot's message if possible.
3. Store the Discord message ID in `receipt_index`.

### Reaction Event Handling

Implement:

```python
on_raw_reaction_add(payload)
```

Use raw reaction events so the message does not need to be cached.

Flow:

1. Ignore bot reactions.
2. Check emoji matches configured read receipt emoji.
3. Look up `payload.message_id` in `receipt_index`.
4. Verify reacting user ID matches `discord_user_id` in the receipt index.
5. Verify `read_receipt_sent` is false.
6. Verify binding still exists and `binding.can_send` is true.
7. Send `receipt.read.v1` packet back to original sender.
8. Mark `read_receipt_sent=true`.
9. Save state.
10. Optionally remove the user's reaction or leave it visible.

### Receipt Type

```text
receipt.read.v1
```

### Receipt Data

```json
{
  "receipt_id": "uuid",
  "for_packet_id": "original-packet-id",
  "receipt_type": "reaction_read",
  "read_at": "2026-04-25T13:05:00Z",
  "discord_user_id": "123456789",
  "discord_message_id": "..."
}
```

### Receipt Headers

```json
{
  "arqs_envelope": "v1",
  "arqs_type": "receipt.read.v1",
  "content_type": "application/json",
  "content_transfer_encoding": "utf-8",
  "content_encoding": "identity",
  "encryption": "none",
  "correlation_id": "original-correlation-id-if-present",
  "causation_id": "original-packet-id"
}
```

---

## Receipt Safety Rules

* Never send receipts for receipt packets.
* Never send read receipts automatically without user action.
* Never claim `discord_delivered` means the user read the message.
* Never send receipts if `binding.can_send` is false.
* Never allow a different Discord user to trigger read receipts for another user's forwarded message.
* Do not send duplicate read receipts for the same forwarded message.
* Preserve original `correlation_id` when available.
* Set `causation_id` to the original ARQS packet ID.

---

## Link Reconciliation

Run a periodic reconciliation task every 30-60 seconds.

Flow:

1. List active ARQS links through AppKit's client.
2. Match active links against pending local endpoints.
3. Convert newly active pending links into bindings.
4. Calculate `can_send` and `can_receive` for each binding.
5. Remove expired pending links.
6. Detect severed/deleted links.
7. Mark or remove severed bindings.
8. Clear active contacts pointing at invalid bindings.
9. Notify Discord users when links activate or sever.
10. Save state.

---

## Newly Activated Pending Link Behavior

When a pending link becomes active:

1. Determine remote endpoint.
2. Calculate direction.
3. Create binding.
4. Remove pending link.
5. Set active contact for the Discord user if appropriate.
6. Notify user.

Notification example:

```text
A new ARQS link is now active as Contact 1.
Direction: receive-only
Can send: no
Can receive: yes
```

If this is the user's second contact, send an explanation:

```text
You now have more than one linked contact.

How sending works:
- Reply to one of my forwarded messages to answer that contact directly.
- Or use /use_contact to choose the active contact for normal messages.

Useful commands:
- /links
- /use_contact
- /current_contact
```

---

## Severed Link Behavior

When a link is no longer active:

1. Mark binding as severed or remove it.
2. Clear active contact if it pointed to the severed binding.
3. Remove reply-index and receipt-index entries for that binding.
4. Notify Discord user.

Notification example:

```text
Your ARQS link server-agent was severed. Messages will not deliver for that contact until you create a new link.
```

---

## Error Handling

### User-Facing Errors

Send concise Discord messages for expected user errors:

* no contacts linked
* multiple contacts and no active contact
* contact not found
* contact cannot send because link is receive-only
* link code redeem failed
* request link code failed
* command send failed
* receipt unavailable because no reverse route exists

### Logs

Use standard Python `logging`.

Recommended logger names:

```text
arqs.discord
arqs.discord.bot
arqs.discord.state
arqs.discord.receipts
arqs.discord.links
```

Do not log API keys or Discord tokens.

---

## Startup Flow

1. Load `.env`.
2. Read `DISCORD_BOT_TOKEN`.
3. Load AppKit config.
4. Create `ARQSApp` for `discord-adapter`.
5. Run AppKit setup/ensure-ready behavior.
6. Load `discord_state.json`.
7. Create Discord bot.
8. Register slash commands.
9. Run Discord bot.
10. In `on_ready`, start:

    * ARQS polling task
    * link reconciliation task
    * optional AppKit outbox worker

---

## Shutdown Flow

On shutdown:

1. Cancel polling task.
2. Cancel reconciliation task.
3. Stop AppKit workers if started.
4. Save Discord bridge state.
5. Close Discord bot.

---

## Discord Intents

Use minimal intents:

```python
intents = discord.Intents.default()
intents.messages = True
intents.dm_messages = True
intents.reactions = True
intents.message_content = False
```

The bot should operate in DMs only.

If Discord requires message content intent for DM content in the target runtime, document that and fail clearly.

---

## Confirmation Views

Use Discord UI views for destructive actions.

For `/delete_link`, use buttons:

```text
Cancel
Delete link
```

Rules:

* Only the requesting Discord user can confirm.
* Confirmation expires after 60 seconds.
* Buttons disable after click.
* If active contact changes before confirm, abort.

---

## AppKit Usage Pattern

Recommended bot setup:

```python
app = ARQSApp.for_app("discord-adapter")
app.setup()
```

Sending typed packets:

```python
app.send_type(
    arqs_type="message.v1",
    body=message_text,
    data={},
    from_endpoint_id=binding.local_endpoint_id,
    to_endpoint_id=binding.remote_endpoint_id,
    delivery_mode="queued",
    content_type="text/plain; charset=utf-8",
    meta={
        "adapter": "discord_dm",
        "discord_user_id": discord_user_id,
        "discord_message_id": discord_message_id,
    },
)
```

Raw ARQS operations that AppKit does not wrap yet may use:

```python
client = app.require_client()
```

Examples:

* create hidden endpoint
* request link code
* redeem link code
* list links
* revoke link
* delete endpoint
* poll inbox
* ACK delivery

---

## Manual Test Checklist

### Startup

* Bot starts with valid config.
* Bot fails clearly if Discord token is missing.
* AppKit identity is created or loaded.
* HTTP/HTTPS behavior follows AppKit config.

### Link Flow

* `/request_link_code bidirectional` creates code.
* `/request_link_code send_only` creates one-way send-only code.
* `/request_link_code receive_only` creates one-way receive-only code.
* `/redeem_link_code` creates binding.
* Pending link becomes active after remote redeem.
* `/links` shows direction correctly.
* `/current_contact` shows direction and receipt mode.

### Sending

* Normal DM sends `message.v1`.
* Replying to forwarded message routes to correct binding.
* Multiple contacts require `/use_contact` unless replying.
* Send to receive-only contact is blocked.
* `/command` sends `command.v1`.
* `/command` to receive-only contact is blocked.

### Receiving

* Inbound `message.v1` forwards to Discord.
* Inbound `notification.v1` renders clearly.
* Unknown packet types render fallback text/data.
* Delivery is ACKed only after Discord send succeeds.
* Unknown local endpoint delivery is ACKed and logged.

### Receipts

* `/receipts status` shows default and contact overrides.
* `/receipts discord_delivered` sends receipt after successful Discord DM send.
* `/receipts reaction_read` adds/uses reaction-based read receipt.
* User reaction sends one `receipt.read.v1`.
* Duplicate reactions do not send duplicate read receipts.
* Other Discord users cannot trigger someone else's read receipt.
* Receipts are skipped for receive-only contacts with no reverse route.

### Persistence

* Restart preserves bindings.
* Restart preserves active contacts.
* Restart preserves receipt settings.
* Restart preserves pending links until expiration.
* Restart preserves queued AppKit outbox messages.

### Link Reconciliation

* Remote redeem activates pending link.
* Severed link is detected.
* User is notified when link is severed.
* Active contact is cleared or updated when a link is severed.

---

## Completion Criteria

The bot v1 is complete when:

1. A Discord user can link an ARQS contact.
2. A Discord user can send ARQS `message.v1` packets by normal DM.
3. A Discord user can receive ARQS `message.v1` and `notification.v1` packets as DMs.
4. A Discord user can send fire-and-forget `command.v1` packets using `/command`.
5. The bot supports bidirectional, send-only, and receive-only ARQS links.
6. The bot blocks outbound sends when the link direction does not allow sending.
7. The bot can send Discord-delivered receipts when enabled.
8. The bot can send reaction-based read receipts when enabled.
9. Receipt behavior is scoped per Discord user and optionally per contact.
10. Inbound ARQS deliveries are ACKed only after successful Discord forwarding.
11. ARQS HTTP/HTTPS policy is handled through AppKit config, not custom Discord bot logic.
12. Discord-specific state is separated from AppKit state.
