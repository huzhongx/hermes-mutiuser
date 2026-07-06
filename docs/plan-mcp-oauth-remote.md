# MCP OAuth Authorization for Remote Users

> ⚠️ **SUPERSEDED (2026-07-06)** by the state-routing implementation in
> `mcp-oauth-state-routing.md` (the original plan here was the basis for it).
> This file is the original design rationale; see the state-routing doc for the
> as-built version.

## Context

Hermes Platform 多用户架构下，MCP OAuth 授权流程只在服务端 localhost 生效。远程用户通过浏览器访问时无法完成 OAuth 授权。需要让远程用户能在前端面板点击 Connect，通过公网回调完成 OAuth。

两个触发场景：
- **场景 A**：用户在 MCP 设置面板主动点击 Connect
- **场景 B**：Agent 调用 MCP 工具时 401，被动触发重新授权

## Implementation Plan

### Step 1: Nginx — 添加 OAuth 回调代理路由

**File**: `/etc/nginx/sites-enabled/openclaw.conf`

在 line 162（`/hermes/dash/` 块之后）添加：

```nginx
# MCP OAuth callback proxy
location ~ ^/hermes/mcp-oauth-cb/([0-9]+)/callback {
    rewrite ^/hermes/mcp-oauth-cb/([0-9]+)/callback(.*)$ /callback$2 break;
    proxy_pass http://127.0.0.1:$1;
    proxy_http_version 1.1;
    proxy_set_header Host 127.0.0.1:$1;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_read_timeout 30s;
}
```

在 line 182，nginx API 路由 regex 中加入 `mcp`：

```
location ~ ^/api/(sessions|upload|files|model|skills|cron|config|env|status|analytics|screenshot|workdirs|mcp) {
```

### Step 2: Broker — 传递公网域名环境变量

**File**: `/opt/hermes-platform/hermes_broker.py` line 422-424

在 `proc_env` 赋值处添加：

```python
proc_env["HERMES_NGINX_DOMAIN"] = os.environ.get("HERMES_NGINX_DOMAIN", "")
```

这样 Hermes 进程能构造公网 redirect_uri。

### Step 3: 修改 redirect_uri 为公网地址

**File**: `/root/.hermes/hermes-agent/tools/mcp_oauth.py`

**3a. `_build_client_metadata()` (line 677)**

将 `redirect_uri = f"http://127.0.0.1:{port}/callback"` 改为：

```python
nginx_domain = os.environ.get("HERMES_NGINX_DOMAIN", "")
if nginx_domain:
    redirect_uri = f"https://{nginx_domain}/hermes/mcp-oauth-cb/{port}/callback"
else:
    redirect_uri = f"http://127.0.0.1:{port}/callback"
```

**3b. `_maybe_preregister_client()` (line 694)**

将重构 redirect_uri 的代码改为直接从 `client_metadata.redirect_uris[0]` 取值（已包含公网地址）。

### Step 4: 捕获 authorization_url 供远程前端获取

**File**: `/root/.hermes/hermes-agent/tools/mcp_oauth.py`

**4a. 新增模块级 pending flows 字典**（line ~100 之后）：

```python
_pending_oauth_flows: dict[str, dict] = {}
_pending_oauth_flows_lock = threading.Lock()
```

**4b. 修改 `_redirect_handler()` (line 400)**

在函数开头捕获 authorization_url：

```python
if _oauth_port is not None:
    with _pending_oauth_flows_lock:
        _pending_oauth_flows[f"flow-{_oauth_port}"] = {
            "authorization_url": authorization_url,
            "callback_port": _oauth_port,
            "status": "awaiting_callback",
            "created_at": time.time(),
        }
```

**4c. 新增访问函数**：

- `get_pending_oauth_authorization_url()` — 返回最近一个 pending flow 的 URL
- `mark_oauth_flow_completed(callback_port)` — 标记完成
- `cleanup_expired_oauth_flows(max_age=600)` — 清理过期

**4d. 在 `_wait_for_callback()` 成功获取 code 后**，调用 `mark_oauth_flow_completed(_oauth_port)`。

### Step 5: 新增 HTTP OAuth 端点

**File**: `/root/.hermes/hermes-agent/hermes_cli/web_server.py` (line 6041 之后)

**5a. `POST /api/mcp/servers/{name}/oauth/start`**

流程：清除旧 token + manager 缓存 → 后台线程调 `_probe_single_server()` → 触发 OAuth flow → `_redirect_handler` 捕获 URL → 前端轮询获取。

**5b. `GET /api/mcp/servers/{name}/oauth/status`**

返回当前 OAuth 流程状态：`initiated` / `awaiting_callback`（含 authorization_url） / `completed` / `failed`。

### Step 6: 工具调用 401 时返回 OAuth URL（场景 B）

**File**: `/root/.hermes/hermes-agent/tools/mcp_tool.py`

在 `_handle_auth_error_and_retry()` 的最终错误分支中（recovery 返回 False 或重试失败后），调用 `get_pending_oauth_authorization_url()` 获取 URL，包含在错误 JSON 中：

```json
{"error": "...", "needs_reauth": true, "server": "xxx", "oauth_url": "https://..."}
```

### Step 7: 前端 UI 改造

**File**: `/opt/hermes-platform/chat.html`

**7a. `showMcpDetail()` (line 3378) — 添加 OAuth Connect 按钮**

当 `s.auth === 'oauth'` 时，在详情面板中显示 "OAuth Authorization" 区域和 Connect 按钮。

**7b. 新增 `startMcpOAuth(name)` 函数**

1. 调用 `POST /api/mcp/servers/{name}/oauth/start`
2. 轮询 `GET /oauth/status` 直到拿到 `authorization_url`
3. `window.open(url)` 打开用户浏览器
4. 继续轮询直到 `status: "completed"` 或超时

**7c. `tool.complete` 事件处理 (line 1409) — 检测 needs_reauth**

解析 `payload.output` JSON，如果含 `needs_reauth: true` 和 `oauth_url`，调用 `showMcpOAuthPrompt()` 在消息区域渲染授权提示卡片。

## Verification

1. 配置一个 OAuth 类型的 MCP server（如需要 auth 的测试 server），确认 `auth: oauth` 在 config.yaml 中
2. 重启 broker + nginx reload
3. 在 MCP 设置面板点击 Connect → 验证弹出授权 URL → 浏览器跳转 → 回调成功 → 状态变为 completed
4. 在对话中让 agent 调用该 server 的工具 → token 过期 → 验证 needs_reauth 提示出现 → 重新授权 → 重试成功
