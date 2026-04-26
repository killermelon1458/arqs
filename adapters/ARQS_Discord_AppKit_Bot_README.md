# ARQS Discord AppKit Bot

`arqs_discord_appkit_bot.py` is the AppKit-backed DM-only Discord bridge described in `ARQS_Discord_Bot_v1_Implementation_Plan.md`.

It uses:

* `appkit` for ARQS runtime setup, identity, transport policy, and outbox behavior
* raw `ARQSClient` access only where AppKit does not yet wrap the needed behavior
* a separate `discord_state.json` file for Discord-specific bindings, reply routing, receipt state, and reaction display state

## Import Behavior

The bot resolves `appkit` from:

1. the repo location used today: `apis/appkit`
2. the current working directory if it contains `appkit/`
3. `./apis` from the current working directory

This lets the bot run from the repo as it exists now, or from a local working directory layout that already has `appkit/`.

## Dependencies

Install:

```bash
pip install -U discord.py python-dotenv
```

## Runtime State

The AppKit app name is:

```text
discord-adapter
```

Default state directory:

```text
~/.arqs/discord-adapter/
```

Important files:

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

## Config

Set the Discord token in the environment:

```bash
export DISCORD_BOT_TOKEN=...
```

If `config.json` does not have a `base_url`, the bot writes a template and exits.

Minimal example:

```json
{
  "app_name": "discord-adapter",
  "base_url": "https://arqs.example.com",
  "node_name": "discord-adapter",
  "default_endpoint_name": "discord-control",
  "default_endpoint_kind": "discord_control",
  "transport_policy": "prefer_https",
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

## Run

From the repo:

```bash
python adapters/arqs_discord_appkit_bot.py
```

With a custom state root:

```bash
python adapters/arqs_discord_appkit_bot.py --state-root /path/to/state-root
```

Force slash-command sync on startup:

```bash
python adapters/arqs_discord_appkit_bot.py --sync-commands
```

## Notes

* The bot is DM-only.
* Inbound ARQS deliveries are ACKed only after Discord forwarding succeeds.
* The bot supports bidirectional, send-only, and receive-only links.
* Discord user reactions on forwarded ARQS messages send `reaction.v1`; reaction adds also send sticky `receipt.read.v1` when the link can send back to ARQS.
* Multiple explicit emoji reactions can be active for the same one-to-one Discord conversation message. Adding one emoji does not remove another; removing one emoji removes only that `reaction_key`.
* Incoming `reaction.v1` packets target the original Discord message when possible and fall back to the latest outbound message for the same contact/user.
* Discord displays `receipt.read.v1` as a synthetic `✅` only when there are no explicit reactions. Explicit reactions replace that marker; removing the last explicit reaction restores `✅` if read state is already true.
* `discord_state.json` stores bindings, pending links, active contacts, reply routing, receipt state, outbound message mapping, and active reactions keyed by `reaction_key`.
* If your Discord runtime does not expose DM message content without the Message Content intent, enable it for the bot application.
