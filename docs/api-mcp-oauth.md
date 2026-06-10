# MCP OAuth 远程授权接口文档

## 概述

为 Hermes Platform 多用户架构新增 MCP OAuth 远程授权支持。远程用户通过 nginx 反向代理完成 OAuth 授权，无需直接访问 Hermes 进程的 localhost。

## 触发场景

### 场景 A：设置面板主动授权

用户在 MCP Servers 设置面板点击 **Connect** 按钮主动发起 OAuth 流程。

```
用户点击 Connect
    ↓
POST /api/mcp/servers/{name}/oauth/start     ← 触发 OAuth flow
    ↓
GET /api/mcp/servers/{name}/oauth/status    ← 轮询获取 authorization_url
    ↓ (status: "awaiting_callback")
window.open(authorization_url)               ← 用户浏览器跳转 OAuth provider
    ↓
用户完成授权 → OAuth provider 回调:
  https://{domain}/hermes/mcp-oauth-cb/{port}/callback?code=X&state=Y
    ↓
nginx 代理 → http://127.0.0.1:{port}/callback
    ↓
GET /api/mcp/servers/{name}/oauth/status    ← 轮询直到完成
    ↓ (status: "completed")
前端显示 "Authorized!"
```

### 场景 B：Agent 工具调用被动授权

Agent 调用 MCP 工具时收到 401，Hermes 自动触发 OAuth 流程，通过 WS 工具完成事件传递授权信息给前端。

```
Agent 调用 MCP tool → 401 Unauthorized
    ↓
Hermes 自动触发 OAuth flow → _redirect_handler 捕获 URL
    ↓
tool.complete 事件 payload.output 含:
  { needs_reauth: true, oauth_url: "https://...", server: "xxx" }
    ↓
前端解析 tool.complete → 调用 showMcpOAuthPrompt()
    ↓
渲染授权提示卡片 → 用户点击 "Authorize in Browser"
    ↓
（同场景 A 的回调流程）
    ↓
用户重试工具调用
```

---

## 一、HTTP 接口

### POST /api/mcp/servers/{name}/oauth/start

发起 OAuth 授权流程。清除旧 token，后台启动 OAuth flow。

**请求**:
```
POST /api/mcp/servers/linear/oauth/start
Authorization: Bearer {ws_token}
```

**响应 `200`**:
```json
{
  "status": "initiated",
  "server_name": "linear"
}
```

**错误**:
| 状态码 | 说明 |
|--------|------|
| 404 | Server '{name}' not found |
| 400 | Server '{name}' does not use OAuth（未配置 `auth: oauth`） |

**内部流程**:
1. 检查 server 是否存在且配置了 `auth: oauth`
2. 清理过期 pending flows
3. 删除已有 token 文件和 manager 内存缓存
4. 后台线程调用 `_probe_single_server()` → 触发 `OAuthClientProvider.async_auth_flow()`
5. `_redirect_handler()` 将 `authorization_url` 存入 `_pending_oauth_flows`
6. OAuth flow 等待用户回调（最多 120s）

---

### GET /api/mcp/servers/{name}/oauth/status

查询 OAuth 流程状态和授权 URL。

**请求**:
```
GET /api/mcp/servers/linear/oauth/status
Authorization: Bearer {ws_token}
```

**响应 `200`**:

状态: **awaiting_callback** — 授权 URL 就绪，等待用户操作
```json
{
  "status": "awaiting_callback",
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "callback_port": 52341
}
```

状态: **completed** — 授权完成，token 已保存
```json
{
  "status": "completed"
}
```

状态: **completed**（含工具列表）— probe 也完成
```json
{
  "status": "completed",
  "tools": [
    {"name": "list_issues", "description": "..."}
  ]
}
```

状态: **failed** — 授权或连接失败
```json
{
  "status": "failed",
  "error": "OAuthNonInteractiveError: non-interactive environment and no cached tokens found"
}
```

状态: **not_started** — 无进行中的 OAuth 流程
```json
{
  "status": "not_started"
}
```

---

### POST /api/mcp/servers/{name}/oauth/revoke

撤销 OAuth 授权。清除已保存的 OAuth token 和内存缓存，服务器配置保留不变，下次使用时需重新授权。

**请求**:
```
POST /api/mcp/servers/linear/oauth/revoke
Authorization: Bearer {ws_token}
```

**响应 `200`**:
```json
{
  "ok": true
}
```

**内部流程**:
1. 调用 `remove_oauth_tokens(name)` — 删除磁盘上的 token 文件和 client info
2. 调用 `MCPOAuthManager.remove(name)` — 清理内存中的 provider 缓存
3. 服务器连接不断开、配置不删除，下次工具调用遇到 401 会自动触发重新授权

---

### DELETE /api/mcp/servers/{name}

删除 MCP 服务器。除清除 OAuth token 外，还会断开运行时连接、注销工具、删除配置。

**请求**:
```
DELETE /api/mcp/servers/linear
Authorization: Bearer {ws_token}
```

**响应 `200`**:
```json
{
  "ok": true
}
```

**响应 `404`**:
```json
{
  "detail": "Server 'linear' not found"
}
```

**内部流程**:
1. 从 config.yaml 删除服务器配置
2. 调用 `disconnect_mcp_server(name)` — 关闭 MCPServerTask、注销工具、清理 circuit breaker 状态
3. 清除 OAuth token 和 manager 内存缓存

---

## 二、工具调用中的 OAuth 信号（场景 B）

当 MCP 工具调用因 401 需要重新授权时，`tool.complete` 事件的 `payload.output` 包含特殊 JSON：

```json
{
  "needs_reauth": true,
  "server": "linear",
  "oauth_url": "https://accounts.google.com/o/oauth2/v2/auth?client_id=...",
  "oauth_callback_port": 52341,
  "error": "MCP server 'linear' requires re-authentication..."
}
```

### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `needs_reauth` | boolean | 标识需要用户介入 OAuth 授权 |
| `server` | string | 需要授权的 MCP server 名称 |
| `oauth_url` | string | OAuth provider 授权 URL，前端可 `window.open()` 打开 |
| `oauth_callback_port` | int | 本地 callback server 端口（仅调试用） |
| `error` | string | 人可读的错误描述 |

### 前端处理

在 `tool.complete` 事件处理器中解析 `payload.output`：

```javascript
if (error && payload.output) {
  const result = JSON.parse(payload.output);
  if (result.needs_reauth && result.oauth_url) {
    showMcpOAuthPrompt(result.server, result.oauth_url);
  }
}
```

`showMcpOAuthPrompt()` 在消息区域渲染授权卡片，用户点击 "Authorize in Browser" 后走场景 A 的回调流程。

---

## 三、Nginx 路由

### OAuth 回调代理

```
https://{domain}/hermes/mcp-oauth-cb/{port}/callback
    → http://127.0.0.1:{port}/callback
```

**路由匹配**: `location ~ ^/hermes/mcp-oauth-cb/([0-9]+)/callback`

callback port 是 `_find_free_port()` 随机分配的高端口（30000-65000），每次 OAuth flow 不同。nginx 通过 URL 路径中的端口号动态路由到正确的 ephemeral callback server。

### MCP API 代理

```
location ~ ^/api/(sessions|upload|files|model|skills|cron|config|env|status|analytics|screenshot|workdirs|mcp) {
    proxy_pass http://hermes_broker;
}
```

`/api/mcp/servers/*` 和 `/api/mcp/servers/*/oauth/*` 请求通过此路由到达 broker，broker 代理到对应 Hermes 进程。

---

## 四、内部机制

### redirect_uri 构造

当 Hermes 进程收到 `HERMES_NGINX_DOMAIN` 环境变量时（broker 在 `_spawn()` 中设置），自动将 OAuth redirect_uri 从 localhost 改为公网地址：

```
无 HERMES_NGINX_DOMAIN (本地模式):
  redirect_uri = "http://127.0.0.1:52341/callback"

有 HERMES_NGINX_DOMAIN (远程模式):
  redirect_uri = "https://huzhongxiang.cloud/hermes/mcp-oauth-cb/52341/callback"
```

该 redirect_uri 会被发送到 OAuth provider 的 Dynamic Client Registration (RFC 7591) 中注册。

### Pending OAuth Flows 注册表

`_pending_oauth_flows` 是 `tools/mcp_oauth.py` 中的模块级字典，记录进行中的 OAuth 流程：

```python
_pending_oauth_flows = {
    "flow-52341": {
        "authorization_url": "https://...",
        "callback_port": 52341,
        "status": "awaiting_callback",  # awaiting_callback | completed | expired
        "created_at": 1718021234.5,
    }
}
```

- `_redirect_handler()` 写入条目（OAuth flow 触发时）
- `_wait_for_callback()` 成功后标记为 `completed`
- `get_pending_oauth_authorization_url()` 返回最新 pending flow
- `cleanup_expired_oauth_flows(600)` 清理超过 10 分钟的过期条目

### 生命周期

```
_redirect_handler() 写入 pending flow
        ↓
_wait_for_callback() 启动 ephemeral HTTP server (127.0.0.1:{port})
        ↓
等待回调 (最长 300s, 每 0.5s 轮询 result dict)
        ↓
callback 到达 → HTTP server 写入 code + state → 标记 completed
        ↓
OAuth SDK 用 code 换取 tokens → HermesTokenStorage 持久化到磁盘
        ↓
ephemeral HTTP server 关闭
```

---

## 五、前端函数

### startMcpOAuth(name)

发起并完成 OAuth 授权流程。

```
1. POST /oauth/start          → 触发后台 OAuth
2. GET /oauth/status (×N)   → 轮询直到 status=awaiting_callback
3. window.open(url)          → 打开用户浏览器
4. GET /oauth/status (×N)   → 轮询直到 status=completed
5. 显示 "Authorized!"
```

超时：获取 URL 最多 30s，等待回调最多 120s。

### showMcpOAuthPrompt(serverName, oauthUrl)

在消息区域显示授权提示卡片，包含 "Authorize in Browser" 按钮。用户点击后走回调流程。

### revokeMcpOAuth(name)

撤销 OAuth 授权并清除 token。

```
1. confirm()                    → 用户确认
2. POST /oauth/revoke           → 清除 token + 内存缓存
3. 刷新 MCP 面板
```

配置保留，服务器不断，下次使用自动触发重新授权。

### removeMcpServer(name)

完全删除 MCP 服务器。

```
1. confirm()                    → 用户确认
2. DELETE /api/mcp/servers/{name} → 断开 + 清 token + 删配置
3. 刷新 MCP 面板
```

---

## 六、配置示例

### config.yaml

```yaml
mcp_servers:
  linear:
    url: "https://mcp.linear.app/mcp"
    auth: oauth              # 关键：声明使用 OAuth
    oauth:
      client_id: "pre-registered-id"  # 可选，跳过 DCR
      scope: "read write"            # 可选
```

### 环境变量

| 变量 | 设置方 | 作用 |
|------|--------|------|
| `HERMES_NGINX_DOMAIN` | broker 启动时 | 公网域名，用于构造 redirect_uri |
| `HERMES_NGINX_DOMAIN` | broker `_spawn()` 传递 | Hermes 进程中 `mcp_oauth.py` 读取 |

### nginx

已有路由无需额外配置，`/hermes/mcp-oauth-cb/` 路由已自动匹配所有端口号。

---

## 七、错误处理

| 场景 | 表现 | 恢复方式 |
|------|------|----------|
| 无 HERMES_NGINX_DOMAIN | redirect_uri 为 localhost，回调不可达 | 确保 broker 启动时设置了该环境变量 |
| 用户关闭授权页面 | callback server 超时后返回 "expired" | 前端提示 "Authorization timed out"，用户可重新 Connect |
| OAuth provider 拒绝授权 | callback 收到 error param | 前端显示 "OAuth flow failed" |
| 非交互环境无缓存 token | probe 阶段报 OAuthNonInteractiveError | 需先在交互环境完成首次授权 |
| 并发 OAuth flow | 每个 flow 有独立 callback port 和 flow_id | 多个 server 可同时发起，互不干扰 |
