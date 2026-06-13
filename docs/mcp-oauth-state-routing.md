# MCP OAuth Callback: State-Based Routing (Multi-User)

## Context

Hermes Platform runs multiple Hermes agent processes (one per user/workspace) on ports 9119-9200. Each process can independently initiate MCP OAuth flows (e.g., GitHub Copilot MCP, Notion MCP).

**Problem**: All user configs specify `redirect_port: 47892`. The OAuth callback URL is `https://domain/hermes/mcp-oauth-cb/{port}/callback`, and nginx routes directly to `127.0.0.1:{port}`. But a TCP port can only be bound by ONE process. When user A's process binds 47892, user B's process gets `OSError: [Errno 98] Address already in use` and GitHub MCP fails to connect (3 retries, then gives up).

**Root cause**: The callback URL embeds the port number, so the port must be globally unique AND known at OAuth App registration time (GitHub only accepts exact-match redirect URIs). Dynamic ports can't be pre-registered.

**Solution**: Use a **fixed callback URL** (no port) + **state-based routing**. GitHub OAuth App registers one callback URL. When the callback arrives, the broker looks up the `state` parameter to find the correct Hermes process port and forwards the request.

## Key Design Decisions

- **Fixed callback URL**: `https://domain/hermes/mcp-oauth/callback` (no port in path)
- **State routing file**: Hermes process writes `{state} → {port}` mapping to `/tmp/hermes_oauth_routes/` when OAuth flow starts
- **Broker reads state**: Broker's new `/mcp-oauth/callback` endpoint reads the `state` query param, looks up the port, proxies to `127.0.0.1:{port}/callback`
- **Each process still binds a unique random port** for its local callback server (unchanged), but the port is never exposed in the public URL

## Implementation Plan

### Step 1: nginx — fixed callback route to broker

**File**: `/etc/nginx/sites-enabled/openclaw.conf` (line ~167)

Replace the existing port-based regex location with a fixed path that goes to the broker:

```nginx
# MCP OAuth callback — routed by broker via state parameter
location = /hermes/mcp-oauth/callback {
    proxy_pass http://hermes_broker/mcp-oauth/callback;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-Proto $scheme;
    # Preserve query params (code=, state=)
    proxy_set_header X-Original-URI $request_uri;
    proxy_read_timeout 30s;
}
```

Keep the old `~ ^/hermes/mcp-oauth-cb/([0-9]+)/callback` block temporarily for backward compat during transition, but it will be removed after verification.

### Step 2: mcp_oauth.py — fixed redirect_uri + state route file

**File**: `/root/.hermes/hermes-agent/tools/mcp_oauth.py`

#### 2a. New: state route file writer

After line ~105 (`_pending_oauth_flows_lock`):

```python
_OAUTH_ROUTE_DIR = "/tmp/hermes_oauth_routes"

def _write_state_route(state: str, port: int) -> None:
    """Write state→port mapping so the broker can route callbacks."""
    import os
    try:
        os.makedirs(_OAUTH_ROUTE_DIR, exist_ok=True)
        path = os.path.join(_OAUTH_ROUTE_DIR, state)
        # Atomic write via temp file
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(port))
        os.rename(tmp, path)
    except Exception:
        pass  # non-fatal — local mode won't need routing

def _cleanup_state_route(state: str) -> None:
    """Remove the state route file after flow completes."""
    import os
    try:
        os.unlink(os.path.join(_OAUTH_ROUTE_DIR, state))
    except FileNotFoundError:
        pass
```

#### 2b. Modify `_build_client_metadata()` (line ~964)

Change redirect_uri to fixed path (no port):

```python
def _build_client_metadata(cfg: dict) -> "OAuthClientMetadata":
    port = cfg.get("_resolved_port")
    if port is None:
        raise ValueError("_configure_callback_port() must be called first")
    client_name = cfg.get("client_name", "Hermes Agent")
    scope = cfg.get("scope")
    nginx_domain = os.environ.get("HERMES_NGINX_DOMAIN", "")
    if nginx_domain:
        # Fixed callback path — broker routes via state parameter
        redirect_uri = f"https://{nginx_domain}/hermes/mcp-oauth/callback"
    else:
        redirect_uri = f"http://127.0.0.1:{port}/callback"
    # ... rest unchanged
```

#### 2c. Modify `_redirect_handler()` (line ~527) and `make_redirect_handler()` (line ~597)

Parse `state` from authorization_url and write the route file:

```python
async def _redirect_handler(authorization_url: str) -> None:
    # Parse state from the authorization URL
    from urllib.parse import urlparse, parse_qs
    parsed = urlparse(authorization_url)
    state = parse_qs(parsed.query).get("state", [None])[0]
    
    if _oauth_port is not None and state:
        _write_state_route(state, _oauth_port)
    
    if _oauth_port is not None:
        flow_id = f"flow-{_oauth_port}"
        with _pending_oauth_flows_lock:
            _pending_oauth_flows[flow_id] = {
                "authorization_url": authorization_url,
                "callback_port": _oauth_port,
                "status": "awaiting_callback",
                "server_name": _oauth_server_name,
                "state": state,
                "created_at": time.time(),
            }
    _print_oauth_url(authorization_url, _oauth_port)
```

Same change in `make_redirect_handler(port, server_name)`.

#### 2d. Cleanup in `mark_oauth_flow_completed()` / `mark_oauth_flow_failed()`

When flow completes or fails, delete the state route file:

```python
def mark_oauth_flow_completed(callback_port: int) -> None:
    flow_id = f"flow-{callback_port}"
    with _pending_oauth_flows_lock:
        flow = _pending_oauth_flows.get(flow_id)
        if flow:
            state = flow.get("state")
            if state:
                _cleanup_state_route(state)
            flow["status"] = "completed"
```

### Step 3: Broker — new callback routing endpoint

**File**: `/opt/hermes-platform/hermes_broker.py` (before the catch-all at line ~1799)

```python
@app.api_route("/mcp-oauth/callback", methods=["GET"])
async def mcp_oauth_callback(request: Request):
    """Route OAuth callback to the correct Hermes process via state param."""
    params = dict(request.query_params)
    state = params.get("state")
    code = params.get("code")
    error = params.get("error")
    
    if not state:
        return HTMLResponse(
            "<html><body><h2>OAuth Error</h2><p>Missing state parameter.</p></body></html>",
            status_code=400,
        )
    
    # Look up port from state route file
    import os
    route_file = f"/tmp/hermes_oauth_routes/{state}"
    port = None
    try:
        with open(route_file) as f:
            port = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        pass
    
    if port is None:
        # Fallback: scan all procs for matching state in _pending_oauth_flows
        # (would require IPC — skip for now, return error)
        return HTMLResponse(
            "<html><body><h2>OAuth Error</h2>"
            "<p>Could not find the OAuth session. "
            "It may have expired. Please try authorizing again.</p></body></html>",
            status_code=404,
        )
    
    # Forward to the Hermes process's callback handler
    qs = f"?code={code}&state={state}" if code else f"?error={error}&state={state}"
    url = f"http://127.0.0.1:{port}/callback{qs}"
    try:
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, timeout=10)
            return HTMLResponse(content=resp.text, status_code=resp.status_code)
    except Exception as e:
        logger.warning(f"OAuth callback forward to port {port} failed: {e}")
        return HTMLResponse(
            "<html><body><h2>OAuth Error</h2>"
            "<p>The agent process is not responding. Please try again.</p></body></html>",
            status_code=502,
        )
```

### Step 4: Update GitHub OAuth App

**Manual step**: In GitHub OAuth App settings (`Ov23lifmBsnY847Canol`), update the Authorization callback URL to:

```
https://huzhongxiang.cloud/hermes/mcp-oauth/callback
```

Remove any port-based callback URLs.

### Step 5: Config cleanup

**All user config.yaml files**: Remove `redirect_port: 47892` from the github oauth block (or leave it — it's now ignored since redirect_uri no longer uses the port). The `client_id` and `client_secret` stay.

### Step 6: Generate patch + restart

1. Generate patch: `cd /root/.hermes/hermes-agent && git diff > /opt/hermes-platform/patches/mcp-oauth-state-routing.patch`
2. Commit patch to hermes-platform repo
3. nginx reload
4. Kill all Hermes processes so they restart with new code

## Verification

1. **Single user**: User A opens GitHub MCP → click Connect → browser opens GitHub authorize → approve → callback arrives at `/hermes/mcp-oauth/callback?code=xxx&state=yyy` → broker reads state file → forwards to port → token saved → status shows "completed"

2. **Concurrent users**: User A and User B both initiate GitHub OAuth simultaneously → both have unique random ports → both state files written → both callbacks routed correctly → both succeed

3. **Check state route files**: `ls /tmp/hermes_oauth_routes/` should show files during active flows, cleaned up after completion

4. **Agent log**: No `Address already in use` errors for port 47892

## Files Modified

| File | Change |
|------|--------|
| `/etc/nginx/sites-enabled/openclaw.conf` | New fixed callback route to broker |
| `/root/.hermes/hermes-agent/tools/mcp_oauth.py` | Fixed redirect_uri, state route file write/cleanup |
| `/opt/hermes-platform/hermes_broker.py` | New `/mcp-oauth/callback` endpoint with state-based forwarding |
| GitHub OAuth App settings | Update callback URL (manual) |
| `/opt/hermes-platform/patches/mcp-oauth-state-routing.patch` | New patch |
