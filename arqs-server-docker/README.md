# arqs-server-docker

This is a concrete V1 ARQS server container built from your ARQS plan.

It implements the core server responsibilities the plan calls out:

- public client registration
- API-key actor auth with capability checks
- packet persistence and idempotency
- inbox polling and ACKs
- client link-code issuance and adapter link completion
- client self-service identity operations
- adapter provisioning by short-lived link code
- adapter revocation
- health and stats endpoints
- SQLite persistence inside a Docker volume

## What is in this build

### Actor endpoints
- `POST /register`
- `POST /packets`
- `GET /inbox?wait=30`
- `POST /packet_ack`
- `POST /link_request`
- `POST /link_complete`
- `POST /identity/rotate-key`
- `POST /identity/regenerate-client`
- `DELETE /identity`
- `GET /health`
- `GET /stats`

### Admin endpoints
- `POST /admin/adapter-provision/request`
- `POST /admin/adapters/{actor_id}/revoke`

### Provisioning endpoint
- `POST /adapter-register`

`/adapter-register` is the bootstrap flow introduced by your link-code provisioning amendment. It is intentionally public, but only redeemable with a short-lived single-use provisioning code.

## Important design choices

### 1. Public client registration exists
That is in the plan. This container implements it directly with `POST /register`.

### 2. Adapter provisioning is not admin-password based
That old model is gone here. The flow is:

1. admin requests an adapter provisioning code
2. operator puts that code into the adapter
3. adapter calls `POST /adapter-register`
4. server creates a new adapter actor and returns its API key once

### 3. Revocation is actor-level
Revoking an adapter sets its state to `revoked`. That immediately invalidates the API key without deleting historical records.

### 4. One active adapter per adapter type in V1
This is a deliberate simplification so outbound routing does not duplicate deliveries. You can relax this later if you add explicit adapter instance routing.

### 5. SQLite is fine for V1
For a small self-hosted packet relay with one app worker, SQLite is reasonable. If you later want multiple server replicas, move to Postgres.

## Quick start

```bash
cp .env.example .env
```

Edit `.env` and set at least:

- `ARQS_ADMIN_API_KEY`

Then build and run:

```bash
docker compose up -d --build
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

## Example flow

### 1) Register a client

```bash
curl -X POST http://127.0.0.1:8080/register
```

Response shape:

```json
{
  "actor_id": "uuid",
  "client_id": "uuid",
  "api_key": "secret"
}
```

### 2) Request a client link code

```bash
curl -X POST http://127.0.0.1:8080/link_request \
  -H "Authorization: Bearer CLIENT_API_KEY"
```

### 3) Ask admin for an adapter provisioning code

```bash
curl -X POST http://127.0.0.1:8080/admin/adapter-provision/request \
  -H "Authorization: Bearer YOUR_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"adapter_type":"discord"}'
```

### 4) Provision the adapter using that code

```bash
curl -X POST http://127.0.0.1:8080/adapter-register \
  -H "Content-Type: application/json" \
  -d '{
    "link_code": "ABCDEFGH",
    "adapter_type": "discord",
    "display_name": "Discord Adapter"
  }'
```

### 5) Complete the client-to-target link from the adapter

```bash
curl -X POST http://127.0.0.1:8080/link_complete \
  -H "Authorization: Bearer ADAPTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "link_code": "ABC123",
    "adapter": "discord",
    "external_id": "123456789",
    "filters": {"topics": ["status", "control"]}
  }'
```

### 6) Send a packet from the client

```bash
curl -X POST http://127.0.0.1:8080/packets \
  -H "Authorization: Bearer CLIENT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "version": 1,
    "packet_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "CLIENT_UUID",
    "timestamp": 1710000000,
    "headers": {
      "topic": "status",
      "tags": ["roof", "alerts"]
    },
    "body": "Waiting for rails",
    "data": {"module": "roof-rail-builder"},
    "meta": {}
  }'
```

### 7) Poll the adapter inbox

```bash
curl "http://127.0.0.1:8080/inbox?wait=30" \
  -H "Authorization: Bearer ADAPTER_API_KEY"
```

### 8) ACK a packet

```bash
curl -X POST http://127.0.0.1:8080/packet_ack \
  -H "Authorization: Bearer ADAPTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"packet_id":"11111111-1111-1111-1111-111111111111","status":"handled"}'
```

## Proxy / Cloudflare notes

The app can optionally trust `CF-Connecting-IP` or `X-Forwarded-For` for rate limiting and source identification, but only when:

- `ARQS_TRUST_PROXY_HEADERS=true`
- the direct peer IP matches `ARQS_TRUST_PROXY_CIDRS` or `ARQS_TRUST_PROXY_IPS`

Default is off. That avoids blindly trusting forwarded headers from arbitrary direct clients.

## Limits of this V1

- no Postgres yet
- no OAuth/JWT complexity
- no multi-instance adapter routing
- no admin actor model beyond a static admin API key
- no edge rate-limit rules; only app-side registration throttling

Those are acceptable V1 tradeoffs for the plan you uploaded.
