# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hermes Platform is a multi-user chat platform that manages isolated Hermes AI agent processes. Each user gets a dedicated Hermes dashboard process, connected via WebSocket through nginx reverse proxy.

## Commands

### Start services
```bash
# Full stack (PostgreSQL + Redis + API server)
bash scripts/start.sh

# Broker only (no PG/Redis needed)
python3 hermes_broker.py

# Standalone API server (in-memory mock, no PG/Redis)
python3 api_server_standalone.py
```

### Test
```bash
bash scripts/test_api.sh
```

### Database init
```bash
psql -h 127.0.0.1 -U hermes -d hermes_platform -f scripts/init_db.sql
```

## Architecture

Two deployment modes share the same frontend (`chat.html`) and nginx config:

### Mode 1: Broker (production, currently active)
`chat.html` → `hermes_broker.py` → per-user Hermes processes on ports 9119-9200

- `hermes_broker.py`: Allocates one Hermes dashboard process per user_id. Returns `ws_url` + `http_url` + `ws_token`. Manages warm pool, idle timeout (1800s), port allocation.
- `chat.html` connects directly to the assigned Hermes process via WS for chat, and via HTTP for session listing/message history.

### Mode 2: Platform API (multi-tenant SaaS)
`chat.html` → `api_server.py` → `process_manager.py` + `ws_pool.py` → Hermes processes

- `api_server.py`: Full multi-tenant API with PostgreSQL + Redis. Tenant isolation via API keys, quotas, rate limiting.
- `api_server_standalone.py`: Same API surface but uses `MemoryPG`/`MemoryStore` instead of real PG/Redis. Seed tenants: `sk-alp-haaa0001`, `sk-bet-hbbb0002`.
- `process_manager.py`: Process lifecycle with warm pool, tenant quotas.
- `ws_pool.py`: Persistent WS connections to Hermes processes with auto-reconnect and heartbeat.

### Nginx routing (nginx/hermes.conf → /etc/nginx/sites-enabled/openclaw.conf)
```
/broker/*                    → 127.0.0.1:8080 (Process Broker)
/hermes/ws/{port}/api/ws     → 127.0.0.1:{port} (WS, Origin spoofed to 127.0.0.1)
/hermes/dash/{port}/api/*    → 127.0.0.1:{port} (HTTP REST, Bearer token auth)
/v1/chat/completions         → 127.0.0.1:3000 (OpenAI-compatible)
/v1/messages                 → 127.0.0.1:3000 (Anthropic-compatible)
/chat                        → static chat.html
```

### Hermes Process API
Each Hermes dashboard process (ports 9119-9200) exposes:
- **WS JSON-RPC** at `/api/ws?token={token}`: `session.create`, `session.resume` (takes sessionKey from DB), `session.close`, `prompt.submit` (streaming via `message.start/delta/complete` events, each carries `session_id` for routing)
- **HTTP REST** at `/api/sessions`, `/api/sessions/{key}/messages`, `DELETE /api/sessions/{key}` etc. Auth: `Authorization: Bearer {token}`
- Token = `__HERMES_SESSION_TOKEN__` from dashboard HTML

### Key design decisions
- **Session IDs**: `session.create` returns short sid (8 hex chars, in-memory). `session.status` returns persistent sessionKey (e.g. `20260526_222442_b202db`) stored in state.db. `session.resume` requires the sessionKey, not the short sid.
- **WS event routing**: Events carry `session_id` (short sid). chat.html maps events to conversations via `convByHermesSid()`.
- **Hermes Origin validation**: nginx must set `proxy_set_header Origin http://127.0.0.1:$port` for WS proxy — Hermes validates Origin against its bound host.
- **Delete lifecycle**: `session.close` (WS) releases resources, then `DELETE /api/sessions/{key}` (HTTP) removes from state.db. Must close before delete.

## Environment Variables

| Variable | Default | Used by |
|----------|---------|---------|
| `HERMES_NGINX_DOMAIN` | (empty) | broker — constructs public ws_url/http_url |
| `HERMES_PUBLIC_HOST` | `127.0.0.1` | broker — fallback host if no nginx domain |
| `SESSIONS_ROOT` | `/tmp/hermes_sessions` | process_manager — work directories |
| `BASE_PORT` / `MAX_PORT` | 9119 / 9200 | process_manager, broker |
| `IDLE_TIMEOUT` | 1800 | process_manager, broker — seconds before reclaim |
| `WARM_POOL_SIZE` | 3-5 | process_manager, broker |
