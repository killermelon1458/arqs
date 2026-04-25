# ARQS Messages GUI

This client uses:

- `arqs_messages_gui.py` — Tkinter desktop client
- `arqs_api.py` — loaded from the script directory, the repo's `../apis` folder, or an installed module
- `arqs_conventions.py` — shared client-side packet header/envelope helper loaded from the same places as `arqs_api.py`

## What it does

- register a node identity and save it to disk
- create named endpoints
- request link codes
- redeem link codes
- save link records locally for easy conversation targeting
- send messages
- send convention-compliant `message.v1` packets
- send and auto-reply to convention-compliant `ping.v1` packets
- poll inbox once or keep polling continuously
- ACK incoming deliveries after saving them locally

## Files it saves locally

The GUI stores state in:

- `~/.arqs_messages_gui/identity.json`
- `~/.arqs_messages_gui/config.json`
- `~/.arqs_messages_gui/links.json`
- `~/.arqs_messages_gui/messages.jsonl`
- `~/.arqs_messages_gui/seen_deliveries.json`
- `~/.arqs_messages_gui/pending_link_codes.json`

## Run

```bash
python arqs_messages_gui.py
```

## Notes

- The GUI looks for `arqs_api.py` next to the script first, then in `../apis`, then in installed modules.
- The GUI uses the shared client-side header convention helper from `arqs_conventions.py` when it is available from the script directory or `../apis`.
- The default endpoint created during registration is saved automatically.
- Named endpoints are created through the API. Extra local aliases are stored in the GUI config.
- The GUI applies the header convention only at the client layer. It does not change server routing, authorization, or other API semantics.
- New GUI-originated messages use the v1 client header convention. Legacy packets without those headers are still displayed for compatibility.
