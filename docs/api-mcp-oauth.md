# MCP OAuth 远程授权接口文档

## 概述

为 Hermes Platform 多用户架构新增 MCP OAuth 远程授权支持。远程用户通过 nginx 反向代理完成 OAuth 授权，无需直接访问 Hermes 进程的 localhost。

## 触发场景

### 场景 A：设置面板主动授权

用户在 MCP Servers 设置面板点击 **Connect** 按钮主动发起 OAuth 流程。

```
用户点击 Connect
    ↓
POST /api/mcp/servers/{name}/oauth/start     ← 触发 OAuth flow，等待 authorization_url（≤30s）
    ↓ (status: "awaiting_callback", authorization_url 就绪)
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

发起 OAuth 授权流程。清除旧 token，后台启动 OAuth flow，**同步等待** authorization_url 就绪后直接返回。

**请求**:
```
POST /api/mcp/servers/linear/oauth/start
Authorization: Bearer {ws_token}
```

**响应 `200`**:

状态: **awaiting_callback** — 授权 URL 就绪（最多等待 30s）
```json
{
  "status": "awaiting_callback",
  "authorization_url": "https://accounts.google.com/o/oauth2/v2/auth?...",
  "callback_port": 52341,
  "server_name": "linear"
}
```

状态: **completed** — 授权流程已完成（已有有效 token）
```json
{
  "status": "completed",
  "server_name": "linear"
}
```

状态: **failed** — OAuth flow 失败
```json
{
  "status": "failed",
  "error": "OAuthNonInteractiveError: non-interactive environment and no cached tokens found",
  "server_name": "linear"
}
```

状态: **timeout** — 30s 内未获取到 authorization_url
```json
{
  "status": "timeout",
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
3. 删除已有 token 文件（保留 `client.json` 以复用 redirect_uri 端口）和 manager 内存缓存
4. 后台线程调用 `_connect_server()` → 触发 `OAuthClientProvider` 的 httpx auth flow
5. per-provider 闭包 `make_redirect_handler(port, server_name)` 将 `authorization_url` 存入 `_pending_oauth_flows`
6. 同步轮询最多 30s，等待 `authorization_url` 出现后返回

---

### GET /api/mcp/servers/{name}/oauth/status

查询 OAuth 流程状态。前端轮询此接口判断授权是否完成。

**请求**:
```
GET /api/mcp/servers/linear/oauth/status
Authorization: Bearer {ws_token}
```

**响应 `200`**:

状态: **in_progress** — OAuth flow 进行中，等待回调
```json
{
  "status": "in_progress"
}
```

状态: **awaiting_callback** — 用户浏览器回调尚未到达（仅当 OAuth flow 仍在等待时出现）
```json
{
  "status": "awaiting_callback"
}
```

状态: **completed** — 授权完成，token 已保存
```json
{
  "status": "completed"
}
```

状态: **failed** — 授权或连接失败
```json
{
  "status": "failed"
}
```

**状态值集合**:

| status | 含义 | 出现阶段 |
|--------|------|---------|
| `in_progress` | OAuth flow 启动中 | `/oauth/start` 刚返回 |
| `awaiting_callback` | 等待用户在浏览器完成授权 | 回调到达前 |
| `completed` | 授权成功，token 已保存 | 回调完成或已有 token |
| `failed` | OAuth 流程失败 | token 过期、provider 拒绝等 |
| `timeout` | 获取 authorization_url 超时 | `/oauth/start` 30s 未获取到 URL |

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

callback port 可以是：
- **固定端口**: 在 config.yaml 中通过 `oauth.redirect_port` 指定（如 Notion: 33305, GitHub: 47892）
- **随机端口**: 不指定时由 `_find_free_port()` 分配的高端口（30000-65000）

nginx 通过 URL 路径中的端口号动态路由到正确的 callback server。

### MCP API 代理

```
location ~ ^/api/(sessions|upload|files|model|skills|cron|config|env|status|analytics|screenshot|workdirs|mcp) {
    proxy_pass http://hermes_broker;
}
```

`/api/mcp/servers/*` 和 `/api/mcp/servers/*/oauth/*` 请求通过此路由到达 broker，broker 代理到对应 Hermes 进程。

---

## 四、内部机制

### 持久回调服务器

OAuth callback server 采用**持久化设计**：每个 callback port 在进程生命周期内只启动一个 `HTTPServer` + `serve_forever()` daemon 线程，多个 OAuth flow 复用同一个服务器。

**为什么不用 ephemeral server？**

之前的方案每次 `_wait_for_callback` 创建新 `HTTPServer` + `handle_request()`。前一个 flow 结束后 socket 停在 `CLOSE_WAIT`/`TIME_WAIT`。下一个 flow 的 `bind()` 抛 `OSError: [Errno 98] Address already in use`，导致 flow 以 `OAuthNonInteractiveError` 失败——用户卡在 `awaiting_callback`，nginx 返回 502/504。

**实现**:

- `_callback_servers: dict[int, dict]` — port → {server, thread, current_result, lock}
- `_get_or_create_callback_server(port)` — 懒启动持久 server，进程结束才退出
- `_PersistentCallbackHandler` — 写入 `current_result`（有 lock 保护），回调到达时同步标记 pending flow 状态
- `_reset_callback_result(port)` — 新 flow 开始时清空 result dict
- `HTTPServer.allow_reuse_address = True` — 兼容 TIME_WAIT 残留

### Per-Provider 闭包（解决多 Server 状态污染）

**问题**: 原设计中 `_redirect_handler` 和 `_wait_for_callback` 读模块级全局变量 `_oauth_port` 和 `_oauth_server_name`。当 Notion + GitHub 并发 OAuth 时，后设置的 `_oauth_server_name="github"` 会覆盖前值，导致 Notion 的 `authorization_url` 被标记为 `server_name="github"`，`/oauth/start` 接口返回错误 URL。

**方案**: 为每个 OAuth provider 构建独立的闭包，关闭 per-server 的 port 和 name：

```python
def make_redirect_handler(port: int, server_name: str):
    """返回闭包，不再读模块全局变量"""
    async def _handler(authorization_url: str) -> None:
        flow_id = f"flow-{port}"
        _pending_oauth_flows[flow_id] = {
            "authorization_url": authorization_url,
            "callback_port": port,
            "server_name": server_name,  # 闭包捕获，不读全局
            ...
        }
    return _handler

def make_callback_handler(port: int):
    """返回闭包，关闭 per-server 的 port"""
    async def _handler() -> tuple[str, str | None]:
        return await _wait_for_callback_on_port(port)
    return _handler
```

`mcp_oauth_manager._build_provider()` 调用闭包工厂而非传递全局函数引用，彻底消除多 server 并发时的状态竞争。

### redirect_uri 构造

当 Hermes 进程收到 `HERMES_NGINX_DOMAIN` 环境变量时（broker 在 `_spawn()` 中透传），自动将 OAuth redirect_uri 从 localhost 改为公网地址：

```
无 HERMES_NGINX_DOMAIN (本地模式):
  redirect_uri = "http://127.0.0.1:52341/callback"

有 HERMES_NGINX_DOMAIN (远程模式):
  redirect_uri = "https://huzhongxiang.cloud/hermes/mcp-oauth-cb/52341/callback"
```

### Pending OAuth Flows 注册表

`_pending_oauth_flows` 是 `tools/mcp_oauth.py` 中的模块级字典，记录进行中的 OAuth 流程：

```python
_pending_oauth_flows = {
    "flow-52341": {
        "authorization_url": "https://...",
        "callback_port": 52341,
        "server_name": "linear",         # per-provider 闭包写入
        "status": "awaiting_callback",   # awaiting_callback | completed | failed
        "created_at": 1718021234.5,
    }
}
```

- `make_redirect_handler(port, server_name)` 闭包写入条目（OAuth flow 触发时）
- `_PersistentCallbackHandler` 回调到达时标记为 `completed` 或 `failed`
- `get_pending_oauth_authorization_url(server_name)` 按 server_name 过滤返回最新的 awaiting flow
- `cleanup_expired_oauth_flows(600)` 清理超过 10 分钟的过期条目

### PRM 预发现（Protected Resource Metadata）

部分 OAuth provider（如 Notion）要求 `resource` 参数匹配 canonical resource。SDK 默认从 server_url 推导，可能不正确。

`_discover_prm_sync(server_url)` 在 provider 构建时预取 `/.well-known/oauth-protected-resource`，注入到 provider context，确保 authorize/token 请求中的 `resource` 参数正确。

### GitHub 特殊兼容

**问题 1: 不支持 DCR**

GitHub MCP server (`api.githubcopilot.com`) 不支持 Dynamic Client Registration (`/register` 返回 404)。

**方案**: config.yaml 中预填 `client_id` + `client_secret`，`_maybe_preregister_client()` 直接写入 `*.client.json`，跳过 DCR。

**问题 2: form-encoded token 响应**

GitHub token endpoint 在缺 `Accept: application/json` header 时返回 `application/x-www-form-urlencoded`。MCP SDK 用 `model_validate_json` 解析失败。

**方案**: `HermesMCPOAuthProvider._handle_token_response()` 捕获 JSON 解析异常，fallback 为 `parse_qs` 解析 form-encoded body，手动构建 `OAuthToken`。

### 生命周期

```
/oauth/start → 清除旧 token → 后台线程 _connect_server()
        ↓
OAuth SDK 401 → httpx auth flow → make_redirect_handler 闭包写入 pending flow
        ↓
/oauth/start 同步等待 authorization_url（≤30s）→ 返回给前端
        ↓
window.open(url) → 用户在浏览器完成授权
        ↓
OAuth provider 回调 → nginx 代理 → 持久 callback server (127.0.0.1:{port})
        ↓
_PersistentCallbackHandler 写入 code + state → 标记 pending flow 为 completed
        ↓
_wait_for_callback_on_port(port) 检测到 code → SDK 用 code 换取 tokens
        ↓
HermesTokenStorage 持久化到磁盘（含 expires_at 绝对时间戳）
        ↓
/oauth/status 轮询返回 completed → 前端显示 "Authorized!"
```

---

## 五、前端函数

### startMcpOAuth(name)

发起并完成 OAuth 授权流程。

```
1. POST /oauth/start          → 触发后台 OAuth，直接返回 authorization_url
2. 检查 status:
   - awaiting_callback → window.open(url) → 打开用户浏览器
   - completed → 已有 token，显示 "Authorized!"
   - failed → 显示错误
   - timeout → 显示超时
3. GET /oauth/status (×N)     → 轮询直到 status=completed（最多 300s）
4. 显示 "Authorized!"
```

超时：获取 URL 最多 30s（`/oauth/start`），等待回调最多 300s（与后端 `_wait_for_callback_on_port()` 对齐）。

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
  # DCR 模式（provider 支持动态注册，如 Notion）
  notion:
    url: "https://mcp.notion.com/mcp"
    auth: oauth
    oauth:
      redirect_port: 33305       # 可选，固定 callback 端口

  # 预注册模式（provider 不支持 DCR，如 GitHub）
  github:
    url: "https://api.githubcopilot.com/mcp/"
    auth: oauth
    oauth:
      client_id: "Ov23lifmBsnY847Canol"      # 必填，跳过 DCR
      client_secret: "dda215fd5e3ca..."       # confidential client 需填
      redirect_port: 47892                     # 可选，固定 callback 端口
```

### 环境变量

| 变量 | 设置方 | 作用 |
|------|--------|------|
| `HERMES_NGINX_DOMAIN` | broker 启动时 | 公网域名，用于构造 redirect_uri |
| `HERMES_NGINX_DOMAIN` | broker `_spawn()` 透传 | Hermes 进程中 `mcp_oauth.py` 读取 |

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
| 并发 OAuth flow（多 server） | per-provider 闭包隔离，互不干扰 | 每个 server 有独立的 port + server_name |
| 端口被占用（上一次 flow 残留） | 持久 server 复用 + `allow_reuse_address` | 不再启动新 server，避免 CLOSE_WAIT 竞态 |
| GitHub token 响应非 JSON | `_handle_token_response` fallback 解析 form-encoded | 自动兼容，无需用户干预 |
| GitHub 不支持 DCR | config 中预填 client_id 跳过注册 | 必须在 config.yaml 中配置 client_id/client_secret |

---

## 八、修改文件清单

### hermes-agent 仓库（5 个文件）

| 文件 | 改动 | 说明 |
|---|---|---|
| `tools/mcp_oauth.py` | +411 -17 | 持久回调服务器、pending flows 注册表、per-provider 闭包工厂 (`make_redirect_handler`/`make_callback_handler`)、`_wait_for_callback_on_port(port)` 参数化重构 |
| `tools/mcp_oauth_manager.py` | +176 -4 | PRM 预发现 (`_discover_prm_sync`)、GitHub form-encoded token 兼容 (`_handle_token_response`)、闭包工厂调用替换全局函数引用 |
| `hermes_cli/web_server.py` | +198 -0 | HTTP API: `/oauth/start`（同步返回 URL）、`/oauth/status`（轮询）、`/oauth/revoke`（清除 token） |
| `tools/mcp_tool.py` | +71 -1 | 401 被动触发重授权骨架（`needs_reauth + oauth_url` 返回） |
| `tools/skills_tool.py` | +4 -0 | 辅助改动 |

### hermes-platform 仓库（3 个文件）

| 文件 | 改动 | 说明 |
|---|---|---|
| `chat.html` | +27 -20 | `startMcpOAuth()`: 直接从 `/oauth/start` 拿 URL、`window.open()`、轮询完成状态（300s 超时） |
| `hermes_broker.py` | +3 -1 | `HERMES_NGINX_DOMAIN` 环境变量透传给子进程 + skill symlink 覆盖修复 |
| `docs/api-mcp-oauth.md` | 本文件 | 完整 API 文档 |
