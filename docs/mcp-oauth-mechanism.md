# MCP Server OAuth 授权机制

## 概述

Hermes 支持 **OAuth 2.1 PKCE** 协议的 MCP server 授权。整个流程由三个模块协作：

| 模块 | 职责 |
|------|------|
| `tools/mcp_oauth.py` | OAuth 基础设施：token 持久化、浏览器授权回调、PKCE 流程 |
| `tools/mcp_oauth_manager.py` | 全局 OAuth 状态管理器：token 缓存、磁盘变更检测、401 去重 |
| `tools/mcp_tool.py` | MCP server 连接层：集成 OAuth auth、401 恢复 + 重连 |

---

## 1. 配置方式

在 `config.yaml` 中通过 `auth: oauth` 声明，所有 `oauth` 子字段可选：

```yaml
mcp_servers:
  my_server:
    url: "https://mcp.example.com/mcp"
    auth: oauth                        # 关键：声明使用 OAuth
    oauth:
      client_id: "pre-registered-id"   # 可选，跳过动态注册
      client_secret: "secret"          # 可选，仅 confidential clients
      scope: "read write"              # 可选，默认使用 server 提供的
      redirect_port: 0                 # 可选，0=自动选空闲端口
      client_name: "My Custom Client"  # 可选，默认 "Hermes Agent"
```

---

## 2. OAuth 授权流程

### 首次授权（用户浏览器交互）

```
用户 → Hermes Agent
  │
  ├─ 1. MCP server 连接时检测 auth: oauth
  │     MCPServerTask._run_http() 调用 MCPOAuthManager.get_or_build_provider()
  │
  ├─ 2. OAuthClientProvider 初始化
  │     - 无 client_id → 自动 Dynamic Client Registration (RFC 7591)
  │     - 有 client_id → 跳过注册
  │
  ├─ 3. 浏览器授权（PKCE）
  │     - 构造 authorization_url（含 code_verifier → code_challenge）
  │     - 打开用户浏览器到 OAuth server 的 /authorize 端点
  │     - 启动临时 localhost HTTP server 等待回调
  │
  ├─ 4. 回调接收
  │     - OAuth server 重定向到 http://localhost:{port}/callback?code=xxx&state=xxx
  │     - 本地 HTTP server 接收 authorization code
  │     - 或非交互环境下弹出 paste prompt 让用户手动粘贴
  │
  └─ 5. Token 交换
        - 用 authorization code + code_verifier 换取 access_token + refresh_token
        - HermesTokenStorage 持久化到磁盘
```

### Token 持久化

```
{HERMES_HOME}/mcp-tokens/
  ├── my_server.json          # OAuthToken (access_token, refresh_token, expires_at)
  ├── my_server.client.json   # OAuthClientInformationFull (client_id, client_secret)
  └── my_server.meta.json     # OAuthMetadata (token_endpoint, authorization_endpoint, etc.)
```

关键设计：`set_tokens()` 时额外记录 `expires_at` 绝对时间戳，进程重启后能正确计算剩余 TTL，避免发送过期 token。

---

## 3. Token 自动刷新

OAuth provider 是 `httpx.Auth` 子类，嵌入在 httpx 请求链中。每次 MCP 调用自动检查 token 有效性：

```
MCP tool call → httpx request → OAuthClientProvider.async_auth_flow()
                              │
                              ├─ token 有效 → 直接放行
                              ├─ token 即将过期 → SDK 自动 refresh
                              └─ refresh 失败 → 返回 401 → 触发恢复流程
```

### 401 恢复流程

当工具调用收到 401 时，`_handle_auth_error_and_retry()` 触发恢复：

```
1. MCPOAuthManager.handle_401()（去重：N 个并发 401 只触发 1 次恢复）
   │
   ├─ 检查磁盘 token 文件是否被外部更新（mtime 变化）
   │   → 有 → 强制 SDK 重新加载 → 恢复成功
   │
   └─ 检查 SDK 能否 in-place refresh（有 refresh_token）
       → 能 → SDK 下次请求自动 refresh → 恢复成功
       → 不能 → 返回 needs_reauth 错误给模型

2. 设置 _reconnect_event → MCPServerTask 重新建立 MCP session

3. 重试一次原始操作
```

---

## 4. 磁盘变更检测（跨进程 token 刷新）

参考 Claude Code 的 `invalidateOAuthCacheIfDiskChanged` 设计：

```
外部进程（cron/CLI）写入新 token 到磁盘
         ↓
下次 MCP 调用 → invalidate_if_disk_changed()
         ↓
比较 token 文件的 st_mtime_ns
         ↓
mtime 变化 → 重置 SDK provider._initialized = False
         ↓
SDK 下次 auth_flow 重新从磁盘加载
```

这使得**外部 token 刷新不需要重启进程**。

---

## 5. 非交互环境处理

在非交互环境（如后台 daemon、broker 管理的进程）中：

- **有缓存 token** → 直接使用，自动 refresh，无需浏览器
- **无缓存 token** → 记录 warning，标记 server 为 failed；提示用户先在交互环境中完成授权
- **token 过期且无法 refresh** → 返回 `needs_reauth` 错误，模型会告知用户需要重新授权

---

## 6. 与 stdio transport 的兼容

stdio transport（如 telebot）不走 HTTP，**不需要 OAuth**。`_run_stdio()` 中仍然设置 OAuth auth handler，但实际不会被触发。stdio server 用 `env` 字段传 API key 即可：

```yaml
telebot:
  command: python3
  args: [...]
  env:
    TELEBOT_API_KEY: sk-xxx    # 静态 API key，非 OAuth
```

---

## 7. OAuth Server Metadata 发现

冷启动时（进程重启后首次连接），需要知道 OAuth server 的 `token_endpoint` 等元数据：

```
1. 从磁盘加载缓存的 oauth_metadata（.meta.json）
   → 有 → 直接使用
   → 无 → 执行发现流程

2. 发现流程：
   a. PRM Discovery → GET {server_url}/.well-known/oauth-protected-resource
      → 获取 authorization_servers URL
   b. ASM Discovery → GET {auth_server}/.well-known/oauth-authorization-server
      → 获取 token_endpoint, authorization_endpoint 等
   c. 缓存到 .meta.json，下次冷启动直接读取
```

---

## 8. 关键代码路径

| 功能 | 文件 | 函数/行号 |
|------|------|-----------|
| 配置解析 | `mcp_tool.py` | `MCPServerTask.run()` line 1764 — `config.get("auth")` |
| OAuth provider 构建 | `mcp_oauth_manager.py` | `get_or_build_provider()` line 353 |
| 浏览器授权回调 | `mcp_oauth.py` | `_redirect_handler()` line 400, `_wait_for_callback()` line 451 |
| Token 持久化 | `mcp_oauth.py` | `HermesTokenStorage` class line 218 |
| Token 刷新（httpx Auth） | `mcp_oauth_manager.py` | `HermesMCPOAuthProvider.async_auth_flow()` line 287 |
| 401 恢复 + 重连 | `mcp_tool.py` | `_handle_auth_error_and_retry()` line 2106 |
| 401 去重 | `mcp_oauth_manager.py` | `MCPOAuthManager.handle_401()` line 506 |
| 磁盘变更检测 | `mcp_oauth_manager.py` | `invalidate_if_disk_changed()` line 466 |
| Metadata 发现 | `mcp_oauth_manager.py` | `_prefetch_oauth_metadata()` line 192 |
| Circuit breaker 重置 | `mcp_tool.py` | `_handle_auth_error_and_retry()` line 2195 — 恢复成功后重置 |
