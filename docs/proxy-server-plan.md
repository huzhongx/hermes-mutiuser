# Plan: 封装 Web Proxy Server，隐藏内部服务信息

## 背景

当前 `chat.html` 是纯前端页面，浏览器直接与以下服务通信：
- `POST /broker/sessions` → 返回 `ws_url`（含端口 9119-9200）、`ws_token`、`pid`、`http_url` 等内部信息
- `wss://domain/hermes/ws/{port}/api/ws?token=xxx` → 直接连接 Hermes 进程 WebSocket
- `https://domain/hermes/dash/{port}/api/*` → 直接访问 Hermes Dashboard HTTP API（Bearer token）

**问题**：
1. 端口号（9119-9200）暴露，攻击者可枚举端口尝试未授权访问
2. `ws_token` 暴露在前端 JS 中，可被截获复用
3. `/broker/*` 端点对外可见，可被脚本滥用（即使有 OAuth 保护）
4. `/hermes/ws/*`、`/hermes/dash/*` 路由暴露内部端口映射
5. PID、session_id 等内部信息不应对外可见

## 目标架构

```
浏览器                     Web Proxy (:8080)                内部服务
  │                            │                              │
  │  POST /api/sessions        │                              │
  │  (cookie only)             │                              │
  │──────────────────────────►│  POST /broker/sessions       │
  │                            │─────────────────────────────►│
  │                            │  ◄─ {ws_url, ws_token, ...} │
  │  {session_id}              │                              │
  │  ◄─────────────────────────│                              │
  │                            │                              │
  │  WSS /api/ws               │                              │
  │  (cookie only)             │  WS /api/ws?token=xxx        │
  │──────────────────────────►│─────────────────────────────►│
  │  ◄─ message events ───────│◄─ message events ───────────│
  │                            │                              │
  │  GET /api/sessions         │  GET /api/sessions           │
  │  (cookie only)             │  Bearer token                │
  │──────────────────────────►│─────────────────────────────►│
  │  ◄─ sessions ─────────────│◄─ sessions ─────────────────│
  │                            │                              │
  │  POST /api/upload          │  POST /broker/upload         │
  │  (multipart, cookie)       │                              │
  │──────────────────────────►│─────────────────────────────►│
```

**核心思路**：在 broker 进程内新增一组 `/api/*` 端点，作为唯一的对外入口。所有内部 URL、端口、token 由 proxy 层持有，浏览器只看到 `/api/*`。

## 修改方案

### 方案选择：在 hermes_broker.py 内新增 proxy 路由

不新增独立服务，在现有 broker 的 FastAPI app 中添加 proxy 端点。优势：
- 零新增进程，复用现有 broker 的进程管理能力
- 直接访问 `broker.get(user_id)` 获取 `proc` 对象，无需额外查找
- 共享 OAuth/JWT 认证逻辑

### 新增对外端点（/api/*）

所有端点通过 JWT cookie 认证（OAuth），对外完全不暴露 port/token/pid。

| 方法 | 对外路径 | 内部调用 | 说明 |
|------|---------|---------|------|
| POST | `/api/sessions` | broker.acquire() | 分配/获取进程，返回不透明 session_id |
| GET | `/api/sessions` | hermes HTTP `/api/sessions` | 获取会话列表 |
| GET | `/api/sessions/{key}/messages` | hermes HTTP `/api/sessions/{key}/messages` | 消息历史 |
| DELETE | `/api/sessions/{key}` | hermes HTTP `DELETE /api/sessions/{key}` | 删除会话 |
| GET | `/api/model/info` | hermes HTTP `/api/model/info` | 模型信息 |
| POST | `/api/model/set` | hermes HTTP `/api/model/set` | 切换模型 |
| GET | `/api/model/options` | hermes HTTP `/api/model/options` | 模型选项 |
| GET | `/api/skills` | hermes HTTP `/api/skills` | 技能列表 |
| PUT | `/api/skills/toggle` | hermes HTTP `/api/skills/toggle` | 技能开关 |
| POST | `/api/upload` | save to disk + WS attach | 文件上传 |
| GET | `/api/files/{filename}` | serve from uploads dir | 文件下载 |
| GET | `/api/cron/jobs` | hermes HTTP `/api/cron/jobs` | 定时任务列表 |
| POST | `/api/cron/jobs` | hermes HTTP `/api/cron/jobs` | 创建定时任务 |
| PUT | `/api/cron/jobs/{id}` | hermes HTTP `/api/cron/jobs/{id}` | 更新定时任务 |
| POST | `/api/cron/jobs/{id}/pause` | hermes HTTP `/api/cron/jobs/{id}/pause` | 暂停 |
| POST | `/api/cron/jobs/{id}/resume` | hermes HTTP `/api/cron/jobs/{id}/resume` | 恢复 |
| POST | `/api/cron/jobs/{id}/trigger` | hermes HTTP `/api/cron/jobs/{id}/trigger` | 手动触发 |
| DELETE | `/api/cron/jobs/{id}` | hermes HTTP `/api/cron/jobs/{id}` | 删除 |
| WebSocket | `/api/ws` | proxy WS → hermes process | 实时对话（双向转发） |
| GET | `/api/status` | hermes HTTP `/api/status` | 状态（公开） |
| GET | `/api/config` | hermes HTTP `/api/config` | 配置 |
| PUT | `/api/config` | hermes HTTP `/api/config` | 更新配置 |
| GET | `/api/env` | hermes HTTP `/api/env` | 环境变量 |
| PUT | `/api/env` | hermes HTTP `/api/env` | 设置环境变量 |
| DELETE | `/api/env` | hermes HTTP `/api/env` | 删除环境变量 |

### 核心实现细节

#### 1. WebSocket Proxy（最关键）

浏览器连接 `wss://domain/api/ws`（无 token 参数，通过 cookie 认证），proxy 层：

```
浏览器 ←WS→ Proxy ←WS→ Hermes Process (localhost:{port}/api/ws?token=xxx)
```

- 认证：从 cookie 解析 JWT → 获取 user_id → 查找对应 proc
- 建立到 Hermes 的 WS 连接：`ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}`
- 双向转发：`browser → proxy → hermes`，`hermes → proxy → browser`
- RPC ID 保持不变（透传）
- 断线处理：任一侧断开，关闭另一侧

```python
from fastapi import WebSocket, WebSocketDisconnect

@app.websocket("/api/ws")
async def ws_proxy(websocket: WebSocket):
    user = _verify_session_cookie(websocket)  # from cookie
    proc = broker.get(user["sub"])
    if not proc:
        await websocket.close(code=4004, reason="No session")
        return

    await websocket.accept()

    # Connect to Hermes process
    hermes_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
    async with aiohttp.ClientSession() as session:
        async with session.ws_connect(hermes_url) as hermes_ws:
            async def forward_to_hermes():
                try:
                    while True:
                        data = await websocket.receive_text()
                        await hermes_ws.send_str(data)
                except WebSocketDisconnect:
                    await hermes_ws.close()

            async def forward_to_browser():
                try:
                    async for msg in hermes_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await websocket.send_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                except Exception:
                    pass

            await asyncio.gather(
                forward_to_hermes(),
                forward_to_browser(),
                return_exceptions=True,
            )
```

#### 2. HTTP Proxy

统一的内部 HTTP 请求转发函数：

```python
async def _hermes_proxy(request: Request, method: str, path: str) -> Response:
    user = _require_session_user(request)
    proc = broker.get(user["sub"])
    if not proc:
        raise HTTPException(404, "No active session")

    url = f"http://127.0.0.1:{proc.port}{path}"
    headers = {"Authorization": f"Bearer {proc.ws_token}"}

    if method in ("POST", "PUT"):
        headers["Content-Type"] = request.headers.get("content-type", "application/json")
        body = await request.body()
        resp = await aiohttp.request(method, url, headers=headers, data=body)
    else:
        resp = await aiohttp.request(method, url, headers=headers)

    return Response(
        content=await resp.read(),
        status_code=resp.status,
        media_type=resp.headers.get("content-type"),
    )
```

#### 3. POST /api/sessions（替代 POST /broker/sessions）

```python
@app.post("/api/sessions")
async def proxy_acquire(request: Request):
    user = _require_session_user(request)
    proc = await broker.acquire(user["sub"])
    # 返回精简信息，不含 port/pid/ws_token/ws_url/http_url
    return {
        "user_id": proc.user_id,
        "status": proc.status,
        "created_at": proc.created_at,
    }
```

#### 4. POST /api/upload（替代 POST /broker/upload）

与现有 `/broker/upload` 逻辑相同，但认证通过 JWT cookie。

#### 5. GET /api/files/{filename}（替代 GET /broker/files/{user_id}/{filename}）

通过 JWT cookie 认证，不暴露 user_id 在 URL 中。

### 对 brokerInfo 的改造

`POST /api/sessions` 不再返回 `ws_url`、`http_url`、`ws_token`、`port`、`pid`。前端只需知道连接成功，后续所有请求走 `/api/*`：

| 字段 | 旧（/broker/sessions） | 新（/api/sessions） |
|------|----------------------|-------------------|
| `user_id` | ✓ | ✓ |
| `session_id` | ✓ | ✗ |
| `status` | ✓ | ✓ |
| `pid` | ✓ | ✗ |
| `port` | ✓ | ✗ |
| `ws_url` | ✓ | ✗ |
| `http_url` | ✓ | ✗ |
| `ws_token` | ✓ | ✗ |
| `created_at` | ✓ | ✓ |

### chat.html 改造

#### 连接流程变更

**旧流程**：
```
1. POST /broker/sessions → 获取 brokerInfo（含 ws_url, http_url, ws_token）
2. WS 连接 brokerInfo.ws_url
3. HTTP 请求 brokerInfo.http_url + path（Bearer brokerInfo.ws_token）
```

**新流程**：
```
1. POST /api/sessions → 获取 {user_id, status}（不含敏感信息）
2. WS 连接 wss://domain/api/ws（cookie 自动带上）
3. HTTP 请求 /api/{path}（cookie 自动带上）
```

#### 具体代码变更

1. **`connect()` 函数**：
   - `POST /broker/sessions` → `POST /api/sessions`
   - 不再保存 `brokerInfo` 的敏感字段
   - `connectWS(url)` → `connectWS()` 无参数，连接固定路径 `/api/ws`

2. **`connectWS()` 函数**：
   ```javascript
   // 旧: ws = new WebSocket(brokerInfo.ws_url);
   // 新:
   const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
   ws = new WebSocket(proto + '//' + location.host + '/api/ws');
   ```

3. **`hermesHTTP()` 函数**：
   ```javascript
   // 旧: fetch(brokerInfo.http_url + path, { headers: { Authorization: 'Bearer ' + brokerInfo.ws_token } })
   // 新:
   async function hermesHTTP(method, path, body) {
     const opts = { method, credentials: 'include' };
     if (body) { opts.headers = { 'Content-Type': 'application/json' }; opts.body = JSON.stringify(body); }
     const resp = await fetch(path, opts);  // path 已经是 /api/xxx
     if (!resp.ok) throw new Error('HTTP ' + resp.status);
     const text = await resp.text();
     try { return JSON.parse(text); } catch(e) { return { raw: text }; }
   }
   ```
   - 所有 `hermesHTTP('GET', '/api/xxx')` 调用不变（路径已经是相对路径，但 host 变了）
   - 移除 `Authorization: Bearer` header

4. **`uploadFiles()` 函数**：
   - `POST /broker/upload` → `POST /api/upload`

5. **文件下载链接**：
   - `/broker/files/{user_id}/{filename}` → `/api/files/{filename}`

6. **移除**：
   - `brokerUrl` 输入框（不再需要用户配置 server URL）
   - 所有引用 `brokerInfo.ws_url`、`brokerInfo.http_url`、`brokerInfo.ws_token` 的代码

### Nginx 配置变更

#### 新增路由

```nginx
# ── Web Proxy API (对外唯一入口) ──
location /api/ {
    proxy_pass http://hermes_broker/api/;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    proxy_send_timeout 300s;
}

# WebSocket proxy for /api/ws
location = /api/ws {
    proxy_pass http://hermes_broker/api/ws;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 600s;
    proxy_send_timeout 300s;
}
```

#### 限制/移除路由

```nginx
# 移除或限制以下路由（不再对外暴露）
# location /broker/ { ... }           → 仅保留 127.0.0.1 访问（internal）
# location /hermes/ws/ { ... }        → 仅保留 127.0.0.1 访问（internal）
# location /hermes/dash/ { ... }      → 仅保留 127.0.0.1 访问（internal）
```

可在这些 location 中添加 `allow 127.0.0.1; deny all;` 限制仅本地访问。

### 实施步骤

#### Phase 1: Broker 端 — 新增 proxy 端点

1. 在 `hermes_broker.py` 中新增 `/api/sessions`（POST/GET）、`/api/ws`（WS proxy）端点
2. 新增 `_hermes_proxy()` 统一 HTTP 转发函数
3. 新增 `/api/upload`、`/api/files/{filename}` 端点
4. 新增所有 `/api/*` CRUD 端点（model、skills、cron、config、env）
5. 保持原有 `/broker/*` 端点不变（内部使用，nginx 限制为 127.0.0.1）

#### Phase 2: 前端 — 切换到 proxy API

1. 修改 `connect()` — 改为 `POST /api/sessions`
2. 修改 `connectWS()` — 连接 `/api/ws`，不再使用 `brokerInfo.ws_url`
3. 修改 `hermesHTTP()` — 移除 Bearer token，改用 cookie 认证
4. 修改 `uploadFiles()` — 改为 `POST /api/upload`
5. 修改 `_renderAttachments()` — 改为 `/api/files/{filename}`
6. 移除 `brokerUrl` 输入框
7. 简化 `brokerInfo` 使用 — 只存 `user_id` 和 `status`

#### Phase 3: Nginx — 锁定内部路由

1. 新增 `/api/` 和 `/api/ws` 路由指向 broker
2. `/broker/`、`/hermes/ws/*`、`/hermes/dash/*` 添加 `allow 127.0.0.1; deny all;`

#### Phase 4: 测试验证

1. OAuth 登录 → 自动分配进程 → 创建会话 → 发消息 → 流式响应
2. 刷新页面 → 自动恢复会话（cookie 有效）
3. 文件上传 → 附件展示 → 点击下载
4. 模型切换、技能管理、定时任务
5. 打开 DevTools → Network 面板确认无 port/token/ws_url 暴露
6. 直接访问 `/broker/sessions` → 403 Forbidden
7. 直接访问 `/hermes/ws/9119/` → 403 Forbidden

## 安全对比

| 信息 | 改造前（浏览器可见） | 改造后（浏览器可见） |
|------|-------------------|-------------------|
| 端口号 9119-9200 | ✗ ws_url/http_url 中可见 | ✓ 完全隐藏 |
| ws_token | ✗ brokerInfo.ws_token | ✓ 完全隐藏 |
| pid | ✗ brokerInfo.pid | ✓ 完全隐藏 |
| session_id (internal) | ✗ brokerInfo.session_id | ✓ 完全隐藏 |
| /broker/* 端点 | ✗ 可直接调用 | ✓ 127.0.0.1 only |
| /hermes/ws/{port}/* | ✗ 可直接连接 | ✓ 127.0.0.1 only |
| /hermes/dash/{port}/* | ✗ 可直接访问 | ✓ 127.0.0.1 only |
| WS 认证 | ?token=xxx (URL 参数) | Cookie (HttpOnly) |
| HTTP 认证 | Bearer token (header) | Cookie (HttpOnly) |
