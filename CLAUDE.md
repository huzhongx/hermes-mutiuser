# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Hermes Platform is a multi-user chat platform that manages isolated Hermes AI agent processes. Each user gets a dedicated Hermes dashboard process on ports 9119-9200, connected via WebSocket through nginx reverse proxy. Users authenticate via GitHub OAuth (JWT cookie) or legacy username mode.

## Commands

### Start services
```bash
# Broker with GitHub OAuth (set env vars first)
GITHUB_CLIENT_ID=xxx GITHUB_CLIENT_SECRET=xxx python3 hermes_broker.py

# Broker without OAuth (legacy username mode)
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

- `hermes_broker.py`: Allocates one Hermes dashboard process per user. Returns `ws_url` + `http_url` + `ws_token`. Manages warm pool, idle timeout (1800s), port allocation. Also handles GitHub OAuth (JWT session), file uploads, and auth endpoints.
- `chat.html` connects directly to the assigned Hermes process via WS for chat, and via HTTP for session listing/message history.

### Mode 2: Platform API (multi-tenant SaaS)
`chat.html` → `api_server.py` → `process_manager.py` + `ws_pool.py` → Hermes processes

- `api_server.py`: Full multi-tenant API with PostgreSQL + Redis. Tenant isolation via API keys, quotas, rate limiting.
- `api_server_standalone.py`: Same API surface but uses `MemoryPG`/`MemoryStore` instead of real PG/Redis. Seed tenants: `sk-alp-haaa0001`, `sk-bet-hbbb0002`.

### Nginx routing (nginx/hermes.conf → /etc/nginx/sites-enabled/openclaw.conf)
```
/auth/*                      → 127.0.0.1:8080 (GitHub OAuth)
/broker/*                    → 127.0.0.1:8080 (Process Broker)
/hermes/ws/{port}/api/ws     → 127.0.0.1:{port} (WS, Origin spoofed to 127.0.0.1)
/hermes/dash/{port}/api/*    → 127.0.0.1:{port} (HTTP REST, Bearer token auth)
/hermes/v1/*                 → 127.0.0.1:8642  (Agent API, OpenAI-compatible)
/v1/chat/completions         → 127.0.0.1:3000  (OpenAI-compatible)
/v1/messages                 → 127.0.0.1:3000  (Anthropic-compatible)
/chat                        → static chat.html
```

### Hermes Process API
Each Hermes dashboard process (ports 9119-9200) exposes:
- **WS JSON-RPC** at `/api/ws?token={token}`: `session.create`, `session.resume` (takes sessionKey), `session.close`, `prompt.submit` (streaming via `message.start/delta/complete`), `image.attach`, `input.detect_drop`, `skills.manage`, `session.list`
- **HTTP REST** at `/api/sessions`, `/api/sessions/{key}/messages`, `DELETE /api/sessions/{key}`, `/api/skills`, etc. Auth: `Authorization: Bearer {token}`

### Authentication (Dual Mode)
- **OAuth mode** (when `GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET` set): `/auth/github` → GitHub authorize → `/auth/callback` → JWT cookie (`hermes_session`, HttpOnly, Secure, SameSite=Lax, 7-day expiry). User ID = GitHub login.
- **Legacy mode** (no env vars): User enters username on welcome page. User ID = arbitrary string.
- Broker endpoints prefer JWT cookie, fall back to `body.user_id` or `X-User-ID` header. This ensures direct API calls are unaffected by OAuth.
- `chat.html` auto-detects mode by probing `GET /auth/user` on load.

### File Upload Flow
1. `POST /broker/upload` (multipart) → file saved to `{work_dir}/uploads/`
2. WS `image.attach` (images) or `input.detect_drop` (other files) registers file with Hermes
3. User types text + Enter → `prompt.submit` with prompt containing file path
4. Two-step UX: upload shows pending chip in input area, user must type + Enter to send

### Key design decisions
- **Session IDs**: `session.create` returns short sid (8 hex chars, in-memory). `session.status` returns persistent sessionKey stored in state.db. `session.resume` requires the sessionKey, not the short sid.
- **WS event routing**: Events carry `session_id` (short sid). chat.html maps events to conversations via `convByHermesSid()`.
- **Hermes Origin validation**: nginx must set `proxy_set_header Origin http://127.0.0.1:$port` for WS proxy.
- **Delete lifecycle**: `session.close` (WS) releases resources, then `DELETE /api/sessions/{key}` (HTTP) removes from state.db. Must close before delete.
- **Per-user HERMES_HOME**: `{work_root}/{user_id}/hermes_home/` — stable path surviving reconnects.
- **Markdown rendering**: marked.js + highlight.js with 120ms debounce during streaming. Code blocks have copy buttons.
- **IME compatibility**: Enter key handler checks `!e.isComposing` to avoid sending during IME composition.

## Environment Variables

| Variable | Default | Used by |
|----------|---------|---------|
| `GITHUB_CLIENT_ID` | (empty) | broker — enables OAuth when set |
| `GITHUB_CLIENT_SECRET` | (empty) | broker — enables OAuth when set |
| `HERMES_NGINX_DOMAIN` | (empty) | broker — constructs public ws_url/http_url |
| `HERMES_PUBLIC_HOST` | `127.0.0.1` | broker — fallback host if no nginx domain |
| `SESSIONS_ROOT` | `/tmp/hermes_sessions` | process work directories |
| `BASE_PORT` / `MAX_PORT` | 9119 / 9200 | broker — port range for Hermes processes |
| `IDLE_TIMEOUT` | 1800 | broker — seconds before reclaim |
| `WARM_POOL_SIZE` | 3 | broker — pre-warmed processes |

## Dependencies

- Python 3.10+
- `fastapi`, `uvicorn` — HTTP server
- `pyjwt` — JWT session tokens
- `aiohttp` — async HTTP client (GitHub API)
- `requests` — sync HTTP client (token exchange)
- Hermes Agent installed at `/root/.hermes/hermes-agent/`
