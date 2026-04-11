# ARQS Server V1

This is a Docker-ready reference implementation of the ARQS server, built directly from the uploaded `ARQS Server Plan (Concrete V1)`.

It implements the plan's core model:

- public node registration
- auto-created default endpoint
- endpoint ownership enforcement
- link-code based explicit linking
- directed route enforcement
- packet queueing with at-least-once delivery
- transport-only ACK that deletes packet + delivery
- finite retention with TTL expiry
- SQLite backend
- local admin CLI
- operational guards for packet size, queue caps, duplicate suppression, send rate limiting, and blacklist-aware registration

## Quick start

```bash
cp config.example.toml config.toml
mkdir -p data

docker compose up --build -d
```

Health check:

```bash
curl http://localhost:8080/health
```

## API auth

Authenticated endpoints accept either:

- `X-ARQS-API-Key: <key>`
- `Authorization: Bearer <key>`

## Example flow

### 1. Register node A

```bash
curl -s http://localhost:8080/register   -H 'Content-Type: application/json'   -d '{"node_name":"node-a"}'
```

### 2. Register node B

```bash
curl -s http://localhost:8080/register   -H 'Content-Type: application/json'   -d '{"node_name":"node-b"}'
```

### 3. Request link code from node A

```bash
curl -s http://localhost:8080/links/request   -H 'Content-Type: application/json'   -H 'X-ARQS-API-Key: <NODE_A_KEY>'   -d '{"source_endpoint_id":"<NODE_A_DEFAULT_ENDPOINT>","requested_mode":"bidirectional"}'
```

### 4. Redeem code from node B

```bash
curl -s http://localhost:8080/links/redeem   -H 'Content-Type: application/json'   -H 'X-ARQS-API-Key: <NODE_B_KEY>'   -d '{"code":"ABC123","destination_endpoint_id":"<NODE_B_DEFAULT_ENDPOINT>"}'
```

### 5. Send packet from A to B

```bash
curl -s http://localhost:8080/packets   -H 'Content-Type: application/json'   -H 'X-ARQS-API-Key: <NODE_A_KEY>'   -d '{
    "version": 1,
    "packet_id": "11111111-1111-1111-1111-111111111111",
    "from_endpoint_id": "<NODE_A_DEFAULT_ENDPOINT>",
    "to_endpoint_id": "<NODE_B_DEFAULT_ENDPOINT>",
    "body": "hello",
    "data": {},
    "headers": {},
    "meta": {}
  }'
```

### 6. Poll inbox as B

```bash
curl -s 'http://localhost:8080/inbox?wait=5&limit=50'   -H 'X-ARQS-API-Key: <NODE_B_KEY>'
```

### 7. ACK the delivery as B

```bash
curl -s http://localhost:8080/packet_ack   -H 'Content-Type: application/json'   -H 'X-ARQS-API-Key: <NODE_B_KEY>'   -d '{"delivery_id":"<DELIVERY_ID>","status":"handled"}'
```

## Admin CLI

Run inside the container:

```bash
docker exec -it arqs-server python -m app.cli health
docker exec -it arqs-server python -m app.cli stats
docker exec -it arqs-server python -m app.cli nodes list
docker exec -it arqs-server python -m app.cli queue list
```

Rotate a node key locally:

```bash
docker exec -it arqs-server python -m app.cli nodes rotate-key <node_id>
```

Destructive queue purge example:

```bash
docker exec -it arqs-server python -m app.cli queue purge-node <node_id> --yes
```

## Notes

- Link codes are 6 uppercase alphanumeric characters, single-use, and default to 10 minutes.
- Packet duplicates are suppressed by `packet_id`.
- Polling is at-least-once. Unacked deliveries may be returned again.
- ACK deletes the packet and delivery immediately.
- Expiry cleanup runs automatically on normal API traffic and can also be triggered via admin CLI.
- The server does not interpret payload meaning.
