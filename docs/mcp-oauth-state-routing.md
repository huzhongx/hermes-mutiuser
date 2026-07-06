# MCP OAuth Callback: State-Based Routing (Multi-User)

> **Status: IMPLEMENTED (2026-07-06).** The plan below is the version that was
> actually built. It supersedes the earlier port-based `api-mcp-oauth.md`
> design (which has a multi-user port-collision problem — kept for history).
>
> Patches (against current live code, not the pre-consolidation version):
> - `patches/mcp-oauth-state-routing.patch` — `tools/mcp_oauth.py`
> - `patches/mcp-oauth-broker-callback.patch` — `hermes_broker.py`
> - `patches/mcp-oauth-dashboard-start-status.patch` — `hermes_cli/web_server.py`
> - nginx: `/etc/nginx/sites-available/openclaw.conf` (manual edit)
>
> **Activation still pending:** the running broker (pid 4051843 at time of
> writing) and the live hermes-agent install must be restarted to load the new
> endpoint + code. See "Activation" below.

## Context

Hermes Platform runs multiple Hermes agent processes (one per user/workspace) on
ports 9119-9200. Each process can independently initiate MCP OAuth flows (e.g.
GitHub Copilot MCP, Notion MCP).

**Problem (root cause of the old failures):** The upstream hermes-agent OAuth
client builds a `redirect_uri` of `http://127.0.0.1:{port}/callback` — the
*user's own machine*. For remote/multi-user users the browser redirect never
reaches the server process, so the flow never completes. Evidence: 0
`github.json` token files on disk despite 9 `github.client.json` (the DCR step
succeeded but the code-for-token exchange never happened). Compounding this,
users sharing a registered `redirect_port` collide (`Address already in use`).

**Solution:** When `HERMES_NGINX_DOMAIN` is set, use a **fixed public callback
URL** with **state-based routing**. The OAuth provider redirects to the public
path once; the broker resolves the `state` parameter back to the originating
process's local port and forwards there. Each process still binds its own
random local port, but the port is never exposed publicly, so concurrent users
never collide.

## What changed (live code)

### 1. `tools/mcp_oauth.py`

- **`_redirect_uri(port)`** (new helper): returns
  `https://{HERMES_NGINX_DOMAIN}/hermes/mcp-oauth/callback` when the domain is
  set, else the upstream `http://127.0.0.1:{port}/callback` fallback.
  `_build_client_metadata()` and `_maybe_preregister_client()` both use it now
  (the two hard-coded `127.0.0.1` redirect_uris are gone).
- **State route file** (`_write_state_route` / `_cleanup_state_route`): writes
  `{state} -> {port}` to `/tmp/hermes_oauth_routes/{state}` atomically; cleaned
  on completion/failure/skip.
- **Pending-flow registry** (`register_pending_oauth_flow`,
  `get_pending_oauth_authorization_url`, `mark_oauth_flow_completed`,
  `mark_oauth_flow_failed`): the redirect handler stashes the authorization URL
  + state here. Used both for route cleanup and by the dashboard's
  `/oauth/start` + the scenario-B reauth path in `mcp_tool.py`.
- **`_redirect_handler`**: parses `state` from the SDK's authorization URL,
  writes the route file, registers the pending flow.
- **`_wait_for_callback`**: calls `mark_oauth_flow_completed/_failed` on the
  way out so route files never leak.
- **`build_oauth_auth`**: records `_oauth_server_name` for the registry.
- The SSH paste-fallback guidance is suppressed when a public route is in use
  (the browser reaches the broker normally there).

### 2. `hermes_broker.py`

- New `GET /mcp-oauth/callback`: reads `state` → resolves port from
  `/tmp/hermes_oauth_routes/{state}` → forwards to
  `127.0.0.1:{port}/callback?code=&state=` via the reused `_get_proxy_http()`
  aiohttp session. Returns clear HTML errors for missing state / unknown
  session / dead process.

### 3. `hermes_cli/web_server.py` (dashboard API)

Frontend (`chat.html`) already calls these; they were missing before:
- `POST /api/mcp/servers/{name}/oauth/start` — clears stale tokens, runs a
  background probe (drives the OAuth flow), waits ≤30s for the authorization
  URL, returns `{status, authorization_url}`.
- `GET /api/mcp/servers/{name}/oauth/status` — polls; `completed` when the
  token file appears on disk.

### 4. nginx (`/etc/nginx/sites-available/openclaw.conf`)

Added a fixed route to the broker (the old port-based `mcp-oauth-cb/{port}`
block is kept for backward compat):

```nginx
location = /hermes/mcp-oauth/callback {
    proxy_pass http://hermes_broker/mcp-oauth/callback;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Original-URI $request_uri;
    proxy_read_timeout 30s;
}
```

Backup: `/root/openclaw.conf.bak.mcp-oauth-state-20260706`.

## Manual step: update the OAuth App redirect URL

For each OAuth provider whose MCP server uses `auth: oauth`, set the registered
callback URL to the fixed public path (no port):

- **GitHub Copilot MCP** (OAuth App `Ov23lifmBsnY847Canol`):
  `https://huzhongxiang.cloud/hermes/mcp-oauth/callback`

Remove any old port-based callback URLs from the provider's settings. This must
be done in the provider's own console; it is not code.

## Activation (PENDING — requires restart)

The code + nginx are in place and unit-verified, but the **running broker and
hermes-agent install must be restarted** for the new endpoint and code to load
(the live E2E callback test returns the broker's `404 {"detail":"Not Found"}`
until then). Restart per `memory/reference_broker_restart.md` (drain → SIGKILL
broker → relaunch with `env -i` + `source .env`). Each per-user process picks up
the new hermes-agent code on respawn.

After restart, verify:
1. `curl https://huzhongxiang.cloud/hermes/mcp-oauth/callback?code=x&state=bogus`
   returns the broker's HTML "OAuth session not found" (not `{"detail":"Not Found"}`).
2. A real GitHub MCP Connect from the dashboard produces a `github.json` token
   file under the user's `mcp-tokens/`.

## Verification done so far (no restart)

- `mcp_oauth.py`: full module imports clean; `_redirect_uri` returns the public
  URL under `HERMES_NGINX_DOMAIN=huzhongxiang.cloud` and the localhost fallback
  without it; state route write/read/cleanup works; pending-flow registry
  registers + clears correctly.
- `hermes_broker.py` + `web_server.py`: syntax OK; all referenced symbols
  resolve (`_get_proxy_http`, `aiohttp`, `HermesTokenStorage`, etc.).
- nginx: `nginx -t` passes; `nginx -s reload` done; the public route reaches
  the broker (confirmed by the broker-shaped 404 response).

## Files

| File | Change |
|------|--------|
| `/root/.hermes/hermes-agent/tools/mcp_oauth.py` | public redirect_uri, state route, pending-flow registry |
| `/root/.hermes/hermes-agent/hermes_cli/web_server.py` | `/oauth/start` + `/oauth/status` endpoints |
| `/opt/hermes-platform/hermes_broker.py` | `GET /mcp-oauth/callback` state router |
| `/etc/nginx/sites-available/openclaw.conf` | fixed callback route → broker |
| Provider OAuth App settings | set callback to the fixed public path (manual) |
