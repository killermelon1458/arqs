# ARQS Admin CLI

## Purpose

The ARQS admin CLI is a local, server-side operational interface for inspecting
runtime state and applying administrative changes directly against the ARQS
database used by `arqs-server`.

It is intended for:

- health and runtime inspection
- live runtime setting changes without a server restart
- IP access rule management
- node, endpoint, link, and link-code inspection
- selected destructive or maintenance operations such as revocation and cleanup

It is not a public client interface. Treat it as an operator tool that should
run only in a trusted server environment.

## Scope

The CLI currently supports these command groups:

- `health`
- `stats`
- `ip`
- `ip policy`
- `limits`
- `rate`
- `nodes`
- `endpoints`
- `links`
- `link-codes`
- `cleanup`

## Current Repo Invocation

In the current repository layout, `app.admin_cli` is not installed as a package
entry point. The supported module invocation pattern is:

```bash
cd arqs-server
python3 -m app.admin_cli --help
```

On Windows, the equivalent is typically:

```powershell
cd arqs-server
py -m app.admin_cli --help
```

There is also a legacy compatibility shim:

```bash
python3 -m app.cli --help
```

## Prerequisites

- Python environment with the `arqs-server/requirements.txt` dependencies
  installed
- access to the ARQS server configuration file
- filesystem access to the configured SQLite database path

The CLI reads configuration from:

- `ARQS_CONFIG`, if set
- otherwise `/app/config.toml`

Example:

```bash
cd arqs-server
ARQS_CONFIG=/path/to/config.toml python3 -m app.admin_cli health --json
```

## Operational Behavior

- The CLI initializes the ARQS schema and admin tables on startup.
- The first run will create the `runtime_settings` and `ip_access_rules`
  tables if they do not already exist.
- Runtime settings are seeded from config on first bootstrap, then become
  live-editable through the CLI.
- Commands operate directly on the configured database. Use care with
  destructive actions such as `nodes revoke`, `links revoke`, and
  `cleanup run`.

## Output Format

Most commands return structured data. In practice, structured responses are
printed as JSON, and `--json` should be treated as the standard operator mode.

Recommendation:

```bash
python3 -m app.admin_cli <command> --json
```

## Exit Codes

- `0`: success
- `1`: generic failure
- `3`: validation error
- `4`: not found
- `5`: conflict

Examples:

- invalid IP address input returns exit code `3`
- requesting a missing object returns exit code `4`
- attempting to set `default_ip_policy=deny` without at least one explicit
  allow rule returns exit code `5`

## Command Reference

### `health`

Show overall admin and runtime health.

```bash
python3 -m app.admin_cli health --json
```

Returns:

- application name
- database path
- database file size when available
- current runtime settings
- IP access mode and rule counts
- maintenance cleanup settings

Typical use:

- verify CLI/database connectivity
- bootstrap admin tables
- confirm active runtime settings

### `stats`

#### `stats summary`

Show aggregate transport and access-control counters.

```bash
python3 -m app.admin_cli stats summary --json
```

Returns:

- `nodes_total`
- `endpoints_total`
- `active_links_total`
- `queued_packets_total`
- `queued_bytes_total`
- `active_link_codes_total`
- `default_ip_policy`
- `allowed_ips_total`
- `denied_ips_total`

#### `stats queue-by-node`

Show queued delivery totals grouped by destination node.

```bash
python3 -m app.admin_cli stats queue-by-node --limit 50 --json
```

Options:

- `--limit`: positive integer, default `50`

#### `stats queue-by-endpoint`

Show queued delivery totals grouped by destination endpoint.

```bash
python3 -m app.admin_cli stats queue-by-endpoint --limit 50 --json
```

Options:

- `--limit`: positive integer, default `50`

#### `stats oldest-queued`

Show the oldest currently queued delivery, if one exists.

```bash
python3 -m app.admin_cli stats oldest-queued --json
```

If no active queued delivery exists, the command returns:

```json
{
  "queued": false
}
```

### `limits`

Inspect or update live runtime storage and queue-related limits.

#### `limits show`

```bash
python3 -m app.admin_cli limits show --json
```

Returns:

- `max_storage_bytes`
- `max_packet_bytes`
- `max_queued_packets_per_endpoint`
- `max_queued_bytes_per_endpoint`
- `max_queued_bytes_per_node`
- `max_total_queued_packets`
- `max_total_queued_bytes`
- `max_inbox_batch`
- `long_poll_max_seconds`
- `updated_at`

#### `limits set`

Update one or more live runtime limits.

```bash
python3 -m app.admin_cli limits set \
  --max-storage-bytes 2000000 \
  --max-packet-bytes 262144 \
  --max-inbox-batch 200 \
  --long-poll-max-seconds 30 \
  --json
```

Supported options:

- `--max-storage-bytes`
- `--max-packet-bytes`
- `--max-queued-packets-per-endpoint`
- `--max-queued-bytes-per-endpoint`
- `--max-queued-bytes-per-node`
- `--max-total-queued-packets`
- `--max-total-queued-bytes`
- `--max-inbox-batch`
- `--long-poll-max-seconds`

Notes:

- all supplied values must be positive integers
- at least one setting must be provided
- updates apply to runtime settings without editing the static config file

### `rate`

Inspect or update live runtime send-rate controls.

#### `rate show`

```bash
python3 -m app.admin_cli rate show --json
```

Returns:

- `send_window_seconds`
- `max_sends_per_window`
- `updated_at`

#### `rate set`

```bash
python3 -m app.admin_cli rate set \
  --send-window-seconds 60 \
  --max-sends-per-window 600 \
  --json
```

Supported options:

- `--send-window-seconds`
- `--max-sends-per-window`

Notes:

- both values must be positive integers when supplied
- at least one setting must be provided

### `ip`

Manage dynamic IP access rules stored in the database.

These commands are most relevant when `network.ip_access_mode = "dynamic"`.

#### `ip list`

List configured dynamic IP rules.

```bash
python3 -m app.admin_cli ip list --json
python3 -m app.admin_cli ip list --action allow --json
python3 -m app.admin_cli ip list --action deny --json
```

Options:

- `--action`: one of `allow` or `deny`

#### `ip allow`

Add or update an explicit allow rule.

```bash
python3 -m app.admin_cli ip allow 127.0.0.1 --reason "localhost admin" --json
```

Options:

- positional `ip`
- `--reason`

#### `ip deny`

Add or update an explicit deny rule.

```bash
python3 -m app.admin_cli ip deny 203.0.113.10 --reason "abuse source" --json
```

Alias:

- `ip block`

#### `ip remove`

Remove an explicit IP rule.

```bash
python3 -m app.admin_cli ip remove 203.0.113.10 --json
```

Alias:

- `ip pardon`

Notes:

- IP values are normalized before storage
- invalid IP input returns a validation error
- removing a missing rule returns `not found`

### `ip policy`

Inspect or update the live default policy used in dynamic IP mode.

#### `ip policy show`

```bash
python3 -m app.admin_cli ip policy show --json
```

Returns:

- `default_ip_policy`
- `updated_at`

#### `ip policy set`

```bash
python3 -m app.admin_cli ip policy set --default allow --json
python3 -m app.admin_cli ip policy set --default deny --json
```

Options:

- `--default`: `allow` or `deny`

Guardrail:

- setting the default policy to `deny` requires at least one explicit
  database-backed allow rule, otherwise the command fails with exit code `5`

### `nodes`

Inspect or change node status.

#### `nodes list`

```bash
python3 -m app.admin_cli nodes list --json
python3 -m app.admin_cli nodes list --status active --limit 100 --json
```

Options:

- `--status`: one of `active`, `disabled`, `revoked`
- `--limit`: positive integer, default `100`

#### `nodes show`

```bash
python3 -m app.admin_cli nodes show <node-id> --json
```

Returns:

- basic node metadata
- endpoint count
- active link count
- active link-code count
- queued inbound packet and byte totals

#### `nodes disable`

```bash
python3 -m app.admin_cli nodes disable <node-id> --json
```

Sets node status to `disabled`.

#### `nodes enable`

```bash
python3 -m app.admin_cli nodes enable <node-id> --json
```

Sets node status to `active`.

#### `nodes revoke`

```bash
python3 -m app.admin_cli nodes revoke <node-id> --json
```

Sets node status to `revoked`.

Operational note:

- revocation is an administrative state change on the node record

### `endpoints`

Inspect endpoint records.

#### `endpoints list`

```bash
python3 -m app.admin_cli endpoints list --json
python3 -m app.admin_cli endpoints list --node-id <node-id> --json
python3 -m app.admin_cli endpoints list --status active --limit 100 --json
```

Options:

- `--node-id`
- `--status`: typically `active`, `disabled`, or `revoked`
- `--limit`: positive integer, default `100`

#### `endpoints show`

```bash
python3 -m app.admin_cli endpoints show <endpoint-id> --json
```

Returns:

- basic endpoint metadata
- queued inbound packet and byte totals
- active link count

### `links`

Inspect or revoke link records.

#### `links list`

```bash
python3 -m app.admin_cli links list --json
python3 -m app.admin_cli links list --status active --limit 100 --json
```

Options:

- `--status`: `active` or `revoked`
- `--limit`: positive integer, default `100`

#### `links revoke`

```bash
python3 -m app.admin_cli links revoke <link-id> --json
```

Behavior:

- marks the link as `revoked`
- revokes all directed routes created by that link
- returns the number of routes revoked

### `link-codes`

Inspect link-code records.

#### `link-codes list`

```bash
python3 -m app.admin_cli link-codes list --json
python3 -m app.admin_cli link-codes list --status active --json
python3 -m app.admin_cli link-codes list --status expired --json
```

Options:

- `--status`: `active`, `used`, `expired`, or `revoked`
- `--limit`: positive integer, default `100`

Notes:

- `active` only returns codes that are still unexpired
- `expired` includes both explicitly expired rows and active rows whose
  expiration time has passed

### `cleanup`

Run immediate expiration cleanup.

#### `cleanup run`

```bash
python3 -m app.admin_cli cleanup run --json
```

Typical response includes counts such as:

- `expired_link_codes`
- `expired_packets`
- `pruned_send_events`

Use this when you want to force maintenance cleanup immediately rather than
waiting for normal background timing.

## Recommended Operator Workflows

### Verify runtime state

```bash
cd arqs-server
ARQS_CONFIG=/path/to/config.toml python3 -m app.admin_cli health --json
python3 -m app.admin_cli limits show --json
python3 -m app.admin_cli rate show --json
```

### Enable default-deny IP policy safely

```bash
python3 -m app.admin_cli ip allow 127.0.0.1 --reason "local admin" --json
python3 -m app.admin_cli ip policy set --default deny --json
python3 -m app.admin_cli ip list --json
```

### Inspect queue pressure

```bash
python3 -m app.admin_cli stats summary --json
python3 -m app.admin_cli stats queue-by-node --json
python3 -m app.admin_cli stats queue-by-endpoint --json
python3 -m app.admin_cli stats oldest-queued --json
```

### Revoke a problematic link

```bash
python3 -m app.admin_cli links list --status active --json
python3 -m app.admin_cli links revoke <link-id> --json
```

### Force immediate cleanup

```bash
python3 -m app.admin_cli cleanup run --json
```

## Troubleshooting

### `No module named 'typer'`

The Python dependencies are not installed in the active environment.

### `No module named 'app'`

You are likely running the command from the wrong directory. In the current
repo layout, run from `arqs-server/`, or otherwise ensure the Python import path
includes that directory.

### Config or database path problems

The CLI loads config at startup and uses the configured SQLite database path.
Confirm that:

- `ARQS_CONFIG` points to the expected file
- the configured `storage.db_path` is writable
- the parent directory for the database exists or can be created

### Validation and conflict errors

Common causes:

- invalid IP address
- unsupported `--status` or `--action` value
- `limit` or runtime setting value less than or equal to zero
- attempting to set default IP policy to `deny` before adding an explicit
  allow rule

## Future Improvements

Areas that would improve operator ergonomics further:

- installable package metadata or a console script entry point
- help output that is fully usable without any environment bootstrap concerns
- command examples in the main server README
- importable shell completion or generated reference output
