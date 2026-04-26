# ARQS AppKit

`appkit` is the higher-level ARQS client layer that sits on top of:

* `arqs_api.py` for raw transport
* `arqs_conventions.py` for packet conventions

It is intended for scripts, small services, agents, desktop adapters, and notification senders that want:

* app-local config and identity storage
* friendly contact labels instead of raw endpoint IDs everywhere
* direct, queued, or background sends
* durable retry with a SQLite outbox
* polling and handler dispatch
* command request/response helpers
* notification helpers for normal events and script failures

## Import model

AppKit is a package under `apis/`.

If you place `apis/` on `PYTHONPATH`, imports look like:

```python
from appkit import ARQSApp, Notifier
from appkit.types import ReceivedPacket, SendResult
from appkit.outbox import SQLiteOutbox
```

The package is split so callers can import only what they need.

Examples:

* runtime app: `from appkit import ARQSApp`
* notification-only script: `from appkit import Notifier`
* shared dataclasses only: `from appkit.types import Contact, ReceivedPacket`
* lower-level outbox access: `from appkit.outbox import SQLiteOutbox`

## Public surface

Top-level exports from `appkit`:

* `ARQSApp`
* `Notifier`
* `notifier()`
* `DeliveryMode`
* `RetryPolicy`
* `AckPolicy`
* `Contact`
* `SendResult`
* `ReceivedPacket`
* `CommandContext`
* `CommandResponse`
* `NotificationPayload`
* `OutboxEntry`
* `TransportResolution`
* `TYPE_REACTION_V1`

## State layout

Each app gets its own state directory:

```text
~/.arqs/<app_name>/
  config.json
  identity.json
  contacts.json
  outbox.sqlite3
  inbox.sqlite3
  appkit.log
```

You can override the root with `state_root=...` when constructing `ARQSApp` or `Notifier`.

## Quick start

### Setup an app

```python
from appkit import ARQSApp

app = ARQSApp.for_app(
    "backup-monitor",
    base_url="https://arqs.example.com",
    default_contact="phone",
    default_endpoint_name="notifications",
    default_endpoint_kind="notification",
)

app.setup()
```

This will:

* create the app state directory
* save config
* resolve transport
* register an identity if one does not exist
* load or create the default endpoint

### Send a simple message

```python
from appkit import ARQSApp

app = ARQSApp.for_app("backup-monitor")

result = app.send_message(
    "daily backup finished",
    contact="phone",
)

print(result.status, result.packet_id)
```

### Send a custom typed packet

```python
from appkit import ARQSApp

app = ARQSApp.for_app("minecraft-monitor")

app.send_type(
    arqs_type="minecraft.player_joined.v1",
    body="malachi1458 joined paper-main",
    data={"player": "malachi1458", "server": "paper-main"},
    contact="phone",
)
```

### Send a reaction packet

`send_reaction()` builds a convention-compliant `reaction.v1` packet. It derives the stable `reaction_key`, preserves `correlation_id` when provided, and sets `causation_id` to the packet being reacted to.

```python
from appkit import ARQSApp

app = ARQSApp.for_app("phone-cli")

app.send_reaction(
    for_packet_id="f70d9b16-cd56-4616-bb6b-c30ddc720a47",
    action="set",
    emoji="🔥",
    emoji_name="fire",
    contact="desktop",
)
```

For custom platform emoji without a portable Unicode glyph, pass `emoji_name`, and optionally `emoji_id` and `animated`. The generated body remains readable, for example `reacted with :party_blob:`.

The generated `reaction_key` is stable for the same packet, source platform, source user, and emoji identity. Send `action="remove"` with the same emoji fields to remove only that emoji reaction.

### Send by raw endpoint IDs

```python
from appkit import ARQSApp

app = ARQSApp.for_app("tooling")

app.send_type(
    arqs_type="custom.event.v1",
    body="manual route",
    data={"ok": True},
    from_endpoint_id="LOCAL-ENDPOINT-UUID",
    to_endpoint_id="REMOTE-ENDPOINT-UUID",
)
```

If `contact` is provided, raw endpoint IDs may not also be provided.

## Delivery modes

AppKit supports three send modes through `send_type()`, `send_message()`, `send_reaction()`, `send_notification()`, and `send_command()`.

### `direct`

Sends immediately through `ARQSClient.send_packet()`.

Use it when:

* the caller is long-lived
* the caller can tolerate a send failure immediately
* you do not want local queue persistence

### `queued`

Writes the packet to `outbox.sqlite3`, then immediately tries to flush that specific packet.

Use it when:

* you want durable script-friendly behavior
* the process might exit soon after sending
* you want the same packet ID preserved across retries

This is the default AppKit delivery mode.

### `background`

Writes the packet to `outbox.sqlite3` and returns quickly.

The background outbox worker will flush later.

Use it when:

* the process stays alive
* you want non-blocking sends
* you are already running a service, GUI, adapter, or agent

## Retry behavior

The durable outbox is backed by SQLite and preserves a stable `packet_id` across retries.

Supported retry policies:

* `none`
* `bounded`
* `until_expired`
* `forever`

Current retry classification:

Retryable:

* connection errors
* HTTP `429`
* HTTP `500`
* HTTP `502`
* HTTP `503`
* HTTP `504`

Permanent:

* `ValueError` and other local packet-shape issues
* insecure transport failures
* most other HTTP client errors such as `400`, `401`, `403`, and `404`

Dead-lettered packets remain in `outbox.sqlite3` and can be inspected with `list_dead_letters()` or the CLI.

## Notifications

Use `Notifier` for simple notification-oriented scripts.

### One-off notification

```python
from appkit import Notifier

note = Notifier.for_app("backup-monitor")

note.send_notification(
    title="Backup complete",
    body="Daily backup completed successfully",
    level="success",
    contact="phone",
)
```

### Script success

```python
from appkit import Notifier

note = Notifier.for_app("backup-monitor")

note.send_script_success(
    script="backup_job.py",
    summary="Backup completed successfully",
    data={"duration_seconds": 123},
)
```

### Script failure

```python
from appkit import Notifier

note = Notifier.for_app("backup-monitor")

try:
    raise RuntimeError("backup failed")
except Exception as exc:
    note.send_script_failure(
        script="backup_job.py",
        exc=exc,
        include_traceback=True,
        delivery_mode="queued",
    )
```

Notification packets use `notification.v1`.

Script failure packets use:

* `script.failure.v1`
* `script.failure.traceback.v1`

## Contacts and linking

Contacts are local client-side conveniences stored in `contacts.json`.

Each contact stores:

* a unique label
* local endpoint ID
* remote endpoint ID
* link ID
* timestamps
* status

### Request a link code

```python
from appkit import ARQSApp

app = ARQSApp.for_app("phone-cli")
link_code = app.request_link_code()
print(link_code.code)
```

### Redeem a link code and save a contact

```python
from appkit import ARQSApp

app = ARQSApp.for_app("backup-monitor")
contact = app.redeem_link_code("ABCD-1234", label="phone")
print(contact.remote_endpoint_id)
```

## Receiving packets

### Register typed handlers

```python
from appkit import ARQSApp

app = ARQSApp.for_app("desktop-client")

@app.on("notification.v1")
def handle_notification(packet, ctx):
    print(packet.data)

@app.on("*")
def handle_any(packet, ctx):
    print(packet.arqs_type, packet.text)
```

### Poll once

```python
packets = app.poll_once(wait=0, limit=50)
```

### Poll forever

```python
app.poll_forever(wait=20)
```

### Receiver thread

```python
app.start_receiver_thread(wait=20)

# later
app.stop_receiver_thread()
```

`poll_once()` returns a list of `ReceivedPacket` values.

## ACK policy

Supported policies:

* `after_handler_success`
* `after_store`
* `always`
* `manual`

Current behavior:

### `after_handler_success`

Default behavior.

* handlers run first
* the delivery is ACKed only if handler dispatch completes successfully

### `after_store`

* the packet is written to `inbox.sqlite3`
* the delivery is ACKed before handler dispatch

### `always`

* successful dispatch ACKs normally
* handler failure ACKs with status `"failed"`

### `manual`

* AppKit does not ACK automatically
* the handler must call `ctx.ack()`

## Commands

AppKit supports `command.v1` and `command.response.v1`.

### Register a command handler

```python
from appkit import ARQSApp

app = ARQSApp.for_app("server-agent")

@app.command("ping")
def ping(args, ctx):
    return {"message": "pong"}

@app.command("disk_usage")
def disk_usage(args, ctx):
    return {"path": args.get("path", "/"), "percent": 72}

app.poll_forever(wait=20)
```

### Send a command

```python
from appkit import ARQSApp

app = ARQSApp.for_app("phone-cli")

response = app.send_command(
    contact="server-agent",
    command="disk_usage",
    args={"path": "/mnt/docker"},
    timeout_seconds=30,
)

print(response.ok, response.result)
```

### Fire-and-forget command

```python
app.send_command(
    contact="server-agent",
    command="refresh_cache",
    args={},
    wait_for_response=False,
)
```

Important: `send_command(..., wait_for_response=True)` only waits on an internal event. A response still has to be received by AppKit. In practice that means the process needs an active receive loop or receiver thread while waiting for the response.

## Transport resolution

AppKit probes transport at runtime and then constructs `ARQSClient` with the resolved URL and transport policy.

Supported policies:

* `allow_http`
* `prefer_https`
* `require_https`

Current behavior:

* `prefer_https` upgrades to HTTPS when available
* local and private HTTP-only hosts are allowed under `prefer_https`
* public HTTP-only hosts are rejected under `prefer_https` unless explicitly remembered as `allow_http`
* `require_https` fails if HTTPS is not reachable

Per-host remembered preferences are stored in `config.json` under `transport_preferences`.

## CLI

AppKit includes a small CLI:

```bash
python -m appkit setup --app backup-monitor --base-url https://arqs.example.com
python -m appkit request-link --app phone-cli
python -m appkit redeem-link --app backup-monitor ABCD-1234 --label phone
python -m appkit contacts --app backup-monitor
python -m appkit test-notification --app backup-monitor --title "Test" --body "Hello"
python -m appkit flush-outbox --app backup-monitor
python -m appkit dead-letter --app backup-monitor
```

## Shared dataclasses

If an application only needs AppKit models, import them directly from `appkit.types`.

Available dataclasses and literals:

* `Contact`
* `SendResult`
* `ReceivedPacket`
* `CommandContext`
* `CommandResponse`
* `NotificationPayload`
* `TransportResolution`
* `OutboxEntry`
* `RuntimePaths`
* `DeliveryMode`
* `RetryPolicy`
* `AckPolicy`

## Current limitations

This is the current implementation, not a frozen spec.

Notable limitations:

* there is no end-to-end encryption layer
* there is no websocket or asyncio transport
* notification dedupe metadata is included in payloads, but AppKit does not yet enforce deduplication locally
* `appkit.log` is part of the state layout, but file-based logging is not automatically configured by the package
* command request/response waiting requires the caller to keep a receive loop active

## Module guide

* [app.py](./app.py) contains `ARQSApp`
* [notifier.py](./notifier.py) contains `Notifier`
* [types.py](./types.py) contains shared dataclasses and literals
* [store.py](./store.py) contains config, identity, contact, and inbox storage helpers
* [outbox.py](./outbox.py) contains the SQLite durable outbox
* [receiver.py](./receiver.py) contains polling and handler dispatch
* [commands.py](./commands.py) contains command send/receive behavior
* [transport.py](./transport.py) contains runtime transport resolution
* [cli.py](./cli.py) contains the command-line interface
