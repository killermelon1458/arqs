# ARQS Messages GUI

This bundle contains:

- `arqs_messages_gui.py` — Tkinter desktop client
- `arqs_api.py` — the uploaded ARQS Python API the GUI wraps

## What it does

- register a node identity and save it to disk
- create named endpoints
- request link codes
- redeem link codes
- save link records locally for easy conversation targeting
- send messages
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

- Put both Python files in the same folder.
- The default endpoint created during registration is saved automatically.
- Named endpoints are created through the API. Extra local aliases are stored in the GUI config.
