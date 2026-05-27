# Hermes Platform API 文档

## 概述

Hermes Platform 提供三层 API：

| 层级 | 入口 | 协议 | 认证 |
|------|------|------|------|
| GitHub OAuth | `/auth/*` | HTTP (redirect) | OAuth cookie |
| Process Broker | `/broker/*` | HTTP REST | OAuth cookie（可选）或 `X-User-ID` header |
| Hermes Dashboard | `/api/*` | HTTP REST | Bearer Token |
| Hermes Dashboard | `/api/ws` | WebSocket JSON-RPC | Token (query) |

所有外部请求通过 nginx 反向代理（`https://huzhongxiang.cloud`），内部服务监听 `127.0.0.1`。

---

## 一、GitHub OAuth API

**后端**: `hermes_broker.py` ｜ **外部路径**: `/auth/*`

GitHub OAuth 登录流程，签发 JWT session cookie。

### GET /auth/github

重定向到 GitHub OAuth 授权页。授权后回调到 `/auth/callback`。

**响应**: `307` 重定向到 `https://github.com/login/oauth/authorize?...`

### GET /auth/callback?code={code}&state={state}

GitHub OAuth 回调。用 code 换取 access_token → 获取 GitHub 用户信息 → 签发 JWT → 设置 cookie → 重定向到 `/chat`。

**响应**: `302` 重定向到 `/chat`，并设置 cookie：
- `hermes_session`: JWT token（HttpOnly, Secure, SameSite=Lax, 7 天有效）

### GET /auth/user

查询当前登录用户信息。

**请求**: Cookie 中带 `hermes_session`

**响应** `200`:
```json
{ "sub": "github_login", "name": "Display Name", "avatar": "https://avatars.githubusercontent.com/..." }
```

**错误** `401`: 未登录

### POST /auth/logout

清除 session cookie。

**响应**:
```json
{ "status": "logged_out" }
```

---

## 二、Process Broker API

**后端**: `hermes_broker.py` ｜ **监听**: `127.0.0.1:8080` ｜ **外部路径**: `/broker/*`

负责为用户分配/回收 Hermes dashboard 进程。

### POST /broker/sessions

为用户分配一个 Hermes 进程。如果已有活跃进程则直接返回。

**认证**（二选一）:
- OAuth 模式：Cookie `hermes_session`（JWT），从中提取 `user_id`
- 直连模式：请求体 `{ "user_id": "alice" }` 或 Header `X-User-ID`

**请求**（直连模式）:
```json
{ "user_id": "alice" }
```

**响应** `200`:
```json
{
  "user_id": "alice",
  "session_id": "uuid-string",
  "status": "active",
  "pid": 12345,
  "port": 9119,
  "ws_url": "wss://huzhongxiang.cloud/hermes/ws/9119/api/ws?token=abc123",
  "http_url": "https://huzhongxiang.cloud/hermes/dash/9119",
  "ws_token": "abc123...",
  "created_at": 1717000000.0
}
```

**错误** `503`:
```json
{ "detail": "已达最大用户数 80" }
```

### GET /broker/sessions/{user_id}

查询用户的活跃进程信息。响应同上。

**错误** `404`: `{ "detail": "用户无活跃会话" }`

### DELETE /broker/sessions/{user_id}

回收用户进程（终止 Hermes 进程，释放端口，清理工作目录）。

**响应** `200`:
```json
{ "status": "released", "user_id": "alice" }
```

### POST /broker/upload

上传文件到用户进程的工作目录。文件保存到 `{work_dir}/uploads/` 下，返回本地路径供后续 WS 消息引用。

**认证**: 同 `/broker/sessions`（OAuth cookie 或 `user_id`）

**请求**: `multipart/form-data`，字段 `file` 为文件内容。

**响应** `200`:
```json
{
  "path": "/tmp/hermes_sessions/alice/abc123/uploads/report.pdf",
  "name": "report.pdf",
  "size": 123456
}
```

**错误**:
- `404`: 用户无活跃会话
- `400`: 缺少 `file` 字段或非 multipart 请求

**使用流程**:
1. 上传文件 → 获取 `path`
2. 通过 WS 发送 `image.attach`（图片）或 `input.detect_drop`（其他文件）
3. 用户输入文字 + `prompt.submit` 触发 Hermes 响应，prompt 中包含文件路径

### GET /broker/health

**响应**:
```json
{
  "status": "ok",
  "active_users": 3,
  "warm_pool": 2,
  "free_ports": 75,
  "max_users": 80,
  "public_host": "10.3.0.8"
}
```

### GET /broker/stats

同 `/broker/health` 的统计部分。

---

## 二、Hermes Dashboard HTTP REST API

**后端**: Hermes dashboard 进程 ｜ **端口**: 9119-9200 ｜ **外部路径**: `/hermes/dash/{port}/api/*`

**认证**: 大部分端点使用 `Authorization: Bearer {token}` 或 `X-Hermes-Session-Token: {token}`。
Token 即 Broker 返回的 `ws_token`（与 WS 共用）。少数端点为公开。

### 2.1 系统状态

#### GET /api/status

服务状态，版本，网关运行状态。**公开，无需认证**。

```json
{
  "version": "0.14.0",
  "release_date": "2026.5.16",
  "hermes_home": "/root/.hermes",
  "config_path": "/root/.hermes/config.yaml",
  "config_version": 16,
  "latest_config_version": 24,
  "gateway_running": true,
  "gateway_pid": 427582,
  "gateway_state": "running",
  "gateway_platforms": {
    "feishu": { "state": "connected" },
    "api_server": { "state": "connected" }
  },
  "active_sessions": 2
}
```

#### GET /api/config/defaults

默认配置值。**公开**。

#### GET /api/config/schema

配置 schema 与分类顺序。**公开**。

---

### 2.2 会话管理

#### GET /api/sessions

列出所有会话。

**查询参数**: `limit` (默认 20), `offset` (默认 0)

```json
{
  "sessions": [
    {
      "id": "20260527_100705_b10726",
      "source": "tui",
      "user_id": null,
      "model": "MiniMax-M2.7-highspeed",
      "system_prompt": "...",
      "parent_session_id": null,
      "started_at": 1779847691.73,
      "ended_at": null,
      "end_reason": null,
      "message_count": 10,
      "tool_call_count": 3,
      "input_tokens": 7868,
      "output_tokens": 447,
      "cache_read_tokens": 60221,
      "cache_write_tokens": 20411,
      "reasoning_tokens": 0,
      "billing_provider": "minimax-cn",
      "estimated_cost_usd": 0.0,
      "title": "测试时序",
      "api_call_count": 5
    }
  ]
}
```

#### GET /api/sessions/{session_key}

获取会话详情。`session_key` 为持久化 ID（如 `20260527_100705_b10726`）。

返回上述单个会话对象。

#### GET /api/sessions/{session_key}/messages

获取会话消息历史。

```json
{
  "messages": [
    { "role": "user", "content": "你好" },
    { "role": "assistant", "content": "你好！有什么可以帮你的？" },
    { "role": "tool", "content": "{\"success\": true, ...}" }
  ]
}
```

- `role`: `user` | `assistant` | `tool` | `system`
- `content`: 文本内容，tool 消息为 JSON 字符串

#### GET /api/sessions/search?q={query}

搜索会话内容。返回匹配的消息片段及所属会话。

#### GET /api/sessions/{session_key}/latest-descendant

获取会话链最新后代（用于分支场景）。

#### DELETE /api/sessions/{session_key}

删除会话。活跃会话需先通过 WS `session.close` 关闭。

```json
{ "ok": true }
```

---

### 2.3 模型管理

#### GET /api/model/info

当前模型信息。**公开**。

```json
{
  "model": "MiniMax-M2.7-highspeed",
  "provider": "minimax-cn",
  "effective_context_length": 131072,
  "capabilities": {
    "supports_tools": true,
    "supports_vision": true,
    "supports_reasoning": true,
    "context_window": 131072,
    "max_output_tokens": 16384,
    "model_family": "minimax"
  }
}
```

#### POST /api/model/set

切换模型。

```json
// 请求
{ "scope": "main", "provider": "minimax-cn", "model": "MiniMax-M2.7-highspeed" }

// 响应
{ "ok": true, "scope": "main", "provider": "minimax-cn", "model": "MiniMax-M2.7-highspeed" }
```

#### GET /api/model/options

可用模型列表。

#### GET /api/model/auxiliary

辅助模型（如 summary、title 生成用的模型）信息。

---

### 2.4 配置管理

#### GET /api/config

当前完整配置。

#### PUT /api/config

更新配置。

```json
// 请求
{ "config": { "model": "new-model" } }

// 响应
{ "ok": true }
```

#### GET /api/config/raw

原始 YAML 配置文本。

#### PUT /api/config/raw

通过 YAML 文本更新配置。

---

### 2.5 环境变量

#### GET /api/env

列出所有环境变量（敏感值已脱敏）。

```json
{
  "MINIMAX_API_KEY": {
    "is_set": true,
    "redacted_value": "sk-***abc",
    "description": "MiniMax API Key",
    "is_password": true
  }
}
```

#### PUT /api/env

设置环境变量。`{ "key": "VAR_NAME", "value": "new_value" }`

#### DELETE /api/env

删除环境变量。`{ "key": "VAR_NAME" }`

#### POST /api/env/reveal

查看敏感变量真实值。**需要认证**。

---

### 2.6 网关管理

#### POST /api/gateway/restart

后台重启 Hermes 网关。`{ "ok": true, "pid": 12345, "name": "restart" }`

#### POST /api/hermes/update

后台更新 Hermes。`{ "ok": true, "pid": 12346, "name": "update" }`

#### GET /api/actions/{name}/status

查询后台任务状态。

```json
{
  "name": "restart",
  "running": true,
  "exit_code": null,
  "pid": 12345,
  "lines": ["Restarting gateway..."]
}
```

---

### 2.7 定时任务 (Cron)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/cron/jobs` | 列出定时任务 |
| GET | `/api/cron/jobs/{job_id}` | 查询单个任务 |
| POST | `/api/cron/jobs` | 创建任务 `{prompt, schedule, name, deliver}` |
| PUT | `/api/cron/jobs/{job_id}` | 更新任务 |
| POST | `/api/cron/jobs/{job_id}/pause` | 暂停 |
| POST | `/api/cron/jobs/{job_id}/resume` | 恢复 |
| POST | `/api/cron/jobs/{job_id}/trigger` | 手动触发 |
| DELETE | `/api/cron/jobs/{job_id}` | 删除 |

---

### 2.8 分析统计

#### GET /api/analytics/usage?days=30

按天和模型的用量统计（token、费用、会话数、API 调用数）。

#### GET /api/analytics/models?days=30

按模型的详细分析数据。

---

### 2.9 Profile / Skills / Logs

| 方法 | 路径 | 说明 |
|------|------|------|
| GET/POST | `/api/profiles` | 列出/创建 profile |
| GET/PUT/DELETE | `/api/profiles/{name}` | 管理 profile |
| GET/PUT | `/api/profiles/{name}/soul` | 读写 profile 人格配置 |
| GET | `/api/logs` | 系统日志 |

#### GET /api/skills

列出所有可用技能及其启用状态。

```json
[
  {
    "name": "hermes-agent",
    "description": "Hermes Agent documentation and setup",
    "enabled": true
  },
  {
    "name": "code-assistant",
    "description": "Code review and generation",
    "enabled": false
  }
]
```

#### PUT /api/skills/toggle

启用或禁用指定技能。

```json
// 请求
{ "name": "code-assistant", "enabled": true }

// 响应
{ "ok": true, "name": "code-assistant", "enabled": true }
```

**注意**: HTTP API 仅支持列表和开关操作。安装、搜索、浏览等功能需通过 WS `skills.manage` 方法（见 3.3 节）。

---

### 2.10 OAuth 管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/providers/oauth` | 列出 OAuth 提供商 |
| DELETE | `/api/providers/oauth/{id}` | 移除 |
| POST | `/api/providers/oauth/{id}/start` | 发起 OAuth 流程 |
| POST | `/api/providers/oauth/{id}/submit` | 提交回调 |
| GET | `/api/providers/oauth/{id}/poll/{sid}` | 轮询 token |
| DELETE | `/api/providers/oauth/sessions/{sid}` | 取消 OAuth 会话 |

---

### 2.11 插件管理

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/dashboard/plugins` | 列出插件 |
| GET | `/api/dashboard/plugins/rescan` | 重新扫描 |
| POST | `/api/dashboard/agent-plugins/install` | 安装插件 |
| POST | `/api/dashboard/agent-plugins/{name}/enable` | 启用 |
| POST | `/api/dashboard/agent-plugins/{name}/disable` | 禁用 |
| DELETE | `/api/dashboard/agent-plugins/{name}` | 卸载 |

---

## 三、Hermes Dashboard WebSocket API

**外部路径**: `/hermes/ws/{port}/api/ws?token={token}` ｜ **协议**: JSON-RPC 2.0

### 连接流程

1. 连接 WebSocket
2. 服务端发送 `gateway.ready` 事件
3. 客户端发送 JSON-RPC 请求
4. 服务端返回响应 + 推送事件

### 请求格式

```json
{ "jsonrpc": "2.0", "id": "1", "method": "session.create", "params": {} }
```

### 响应格式

```json
// 成功
{ "jsonrpc": "2.0", "id": "1", "result": { ... } }

// 错误
{ "jsonrpc": "2.0", "id": "1", "error": { "code": -32601, "message": "Method not found" } }
```

---

### 3.1 会话方法

#### session.create

创建新会话。

```json
// 请求
{ "method": "session.create", "params": {} }

// 响应
{
  "session_id": "8ccf397a",
  "info": {
    "model": "MiniMax-M2.7-highspeed",
    "tools": { ... },
    "skills": { ... },
    "cwd": "/tmp/hermes_sessions/user/session",
    "profile_name": "default"
  }
}
```

- `session_id`: 短 ID（8 位 hex），**仅内存有效**，进程重启后失效

#### session.resume

恢复历史会话。**参数为 sessionKey（持久化 ID），不是短 ID**。

```json
// 请求
{ "method": "session.resume", "params": { "session_id": "20260526_222442_b202db" } }

// 响应
{
  "session_id": "a1b2c3d4",
  "resumed": "20260526_222442_b202db",
  "message_count": 6,
  "messages": [ ... ],
  "info": { "model": "...", ... }
}
```

- 返回新的短 `session_id`，原历史消息完整恢复

#### session.status

获取会话状态。**用于获取 sessionKey**。

```json
// 请求
{ "method": "session.status", "params": { "session_id": "8ccf397a" } }

// 响应
{
  "output": "Session: 8ccf397a\nModel: MiniMax-M2.7-highspeed\n...\nSession ID: 20260526_222442_b202db\n..."
}
```

- `output` 为格式化文本，从中解析 `Session ID:` 行获取 sessionKey

#### session.close

关闭活跃会话，释放 agent/worker 资源。

```json
// 请求
{ "method": "session.close", "params": { "session_id": "8ccf397a" } }

// 响应
{ "closed": true }
```

#### session.delete

从 state.db 删除会话记录。**必须先 close 活跃会话**。

```json
// 请求
{ "method": "session.delete", "params": { "session_id": "20260526_222442_b202db" } }

// 响应
{ "deleted": "20260526_222442_b202db" }
```

#### session.list

列出会话。

```json
// 请求
{ "method": "session.list", "params": { "limit": 50 } }

// 响应
{
  "sessions": [
    {
      "id": "20260527_100705_b10726",
      "title": "测试时序",
      "preview": "第一条消息预览...",
      "started_at": 1779847691,
      "message_count": 10,
      "source": "tui"
    }
  ]
}
```

#### session.title

获取或设置会话标题。

```json
// 获取
{ "method": "session.title", "params": {} }
// → { "title": "当前标题", "session_key": "...", "pending": false }

// 设置
{ "method": "session.title", "params": { "title": "新标题" } }
```

#### session.history

获取当前会话的消息历史。

#### session.undo

撤销最后一轮对话。

#### session.compress

压缩历史消息（减少 token 消耗）。

```json
{ "method": "session.compress", "params": { "strategy": "last" } }
// → { "success": true, "old_count": 20, "new_count": 5, "skipped": 0 }
```

#### session.branch

从当前会话创建分支。

#### session.save

强制将历史写入 state.db。

#### session.usage

当前会话 token 用量。

```json
// → { "calls": 5, "input": 7868, "output": 447, "total": 8315 }
```

---

### 3.2 对话方法

#### prompt.submit

发送消息，触发流式响应。

```json
// 请求
{ "method": "prompt.submit", "params": { "session_id": "8ccf397a", "text": "你好" } }

// 响应（立即返回）
{ "status": "streaming" }
```

之后通过事件流接收响应内容。

#### prompt.background

后台提交 prompt（不阻塞主会话）。

```json
{ "method": "prompt.background", "params": { "session_id": "...", "text": "...", "priority": "normal" } }
```

---

### 3.3 Skills 管理

#### skills.manage

统一的技能管理方法，通过 `action` 参数区分操作。

**列出技能**:
```json
{ "method": "skills.manage", "params": { "action": "list" } }
// → { "skills": [{ "name": "...", ... }] }
```

**搜索技能**:
```json
{ "method": "skills.manage", "params": { "action": "search", "query": "code" } }
// → { "results": [{ "name": "code-assistant", "description": "..." }] }
```

**安装技能**:
```json
{ "method": "skills.manage", "params": { "action": "install", "query": "skill-name" } }
// → { "installed": true, "name": "skill-name" }
```

**浏览技能库**:
```json
{ "method": "skills.manage", "params": { "action": "browse", "query": "1", "page_size": 20 } }
// → 分页结果
```

**查看技能详情**:
```json
{ "method": "skills.manage", "params": { "action": "inspect", "query": "skill-name" } }
// → { "info": { ... } }
```

| action | 参数 | 说明 |
|--------|------|------|
| `list` | 无 | 列出所有可用技能 |
| `search` | `query` (必填) | 搜索技能 |
| `install` | `query` (必填，技能名) | 从 hub 安装技能 |
| `browse` | `query` (页码), `page_size` | 分页浏览技能库 |
| `inspect` | `query` (必填，技能名) | 查看技能详情 |

#### skills.reload

重新扫描文件系统，检测新增或移除的技能。

```json
{ "method": "skills.reload", "params": {} }

// →
{
  "output": "Reloading skills...\nAdded skills:\n  - new-skill\n2 skill(s) available",
  "result": {
    "added": [{ "name": "new-skill" }],
    "removed": [],
    "total": 2
  }
}
```

**Skills 功能矩阵**:

| 操作 | HTTP REST | WS JSON-RPC |
|------|-----------|-------------|
| 列出技能 | `GET /api/skills` | `skills.manage {action:"list"}` |
| 启用/禁用 | `PUT /api/skills/toggle` | — |
| 搜索技能 | — | `skills.manage {action:"search"}` |
| 安装技能 | — | `skills.manage {action:"install"}` |
| 浏览技能库 | — | `skills.manage {action:"browse"}` |
| 查看详情 | — | `skills.manage {action:"inspect"}` |
| 重新扫描 | — | `skills.reload` |
| 删除/卸载 | — | ❌ 不支持 |
| 创建自定义 | — | ❌ 不支持（需手动添加文件） |

---

### 3.4 其他方法

| 方法 | 参数 | 说明 |
|------|------|------|
| `terminal.resize` | `{cols, rows}` | 调整终端大小 |
| `clipboard.paste` | `{path?, contents}` | 粘贴内容 |
| `image.attach` | `{path}` | 附加图片 |
| `input.detect_drop` | `{path}` | 检测拖放文件类型 |
| `session.steer` | `{text}` | 向当前 agent 发送引导输入 |
| `config.get` | `{section?, key?}` | 获取配置 |
| `config.set` | `{key_path, value}` | 设置配置 |
| `commands.catalog` | `{type?, search?}` | 列出可用命令 |
| `delegation.status` | `{}` | 委派系统状态 |
| `delegation.pause` | `{}` | 暂停委派 |

---

### 3.5 事件推送

服务端主动推送的事件，格式：

```json
{
  "jsonrpc": "2.0",
  "method": "event",
  "params": {
    "type": "message.delta",
    "session_id": "8ccf397a",
    "payload": { ... }
  }
}
```

`session_id` 用于多会话路由——客户端据此将事件分发到对应会话。

#### 消息事件

| 事件 | payload | 说明 |
|------|---------|------|
| `message.start` | `null` | 开始响应 |
| `message.delta` | `{text, rendered?}` | 流式文本片段 |
| `message.complete` | `{text, warning?, rendered?}` | 响应完成 |

#### 工具事件

| 事件 | payload | 说明 |
|------|---------|------|
| `tool.start` | `{name, args, tool_id}` | 工具调用开始 |
| `tool.complete` | `{tool_id, name, duration_s, summary}` | 工具调用完成 |
| `tool.progress` | `{name, preview}` | 工具进度 |
| `tool.generating` | `{name}` | 工具开始生成输出 |

#### 思考事件

| 事件 | payload | 说明 |
|------|---------|------|
| `thinking.delta` | `{text}` | 思考过程文本 |
| `reasoning.available` | `{text}` | 推理输出可用 |
| `reasoning.delta` | `{text, verbose?}` | 推理过程片段 |

#### 状态与错误

| 事件 | payload | 说明 |
|------|---------|------|
| `status.update` | `{kind, text}` | 状态更新 |
| `error` | `{message}` | 错误 |
| `session.info` | session info 对象 | 会话信息更新 |
| `gateway.ready` | `{skin}` | 连接就绪（首次连接时发送） |

#### 交互事件

| 事件 | payload | 说明 |
|------|---------|------|
| `approval.request` | approval 详情 | 审批请求 |
| `voice.transcript` | `{text}` | 语音转写 |
| `voice.status` | `{state}` | 语音状态 |

---

### 3.6 会话 ID 说明

Hermes 使用两套 ID：

| ID 类型 | 格式示例 | 生命周期 | 用途 |
|---------|---------|---------|------|
| 短 ID (sid) | `8ccf397a` | 仅内存，进程重启失效 | WS 事件路由、prompt.submit |
| sessionKey | `20260526_222442_b202db` | 持久化到 state.db | session.resume、HTTP API、跨重启恢复 |

**获取 sessionKey**: 创建会话后调用 `session.status`，从 `output` 文本中解析 `Session ID:` 行。

---

## 四、Nginx 路由映射

| 外部路径 | 后端 | 说明 |
|----------|------|------|
| `/auth/*` | `127.0.0.1:8080` | GitHub OAuth 登录 |
| `/broker/*` | `127.0.0.1:8080` | Process Broker |
| `/hermes/ws/{port}/*` | `127.0.0.1:{port}` | Dashboard WS (Origin 重写为 127.0.0.1) |
| `/hermes/dash/{port}/*` | `127.0.0.1:{port}` | Dashboard HTTP |
| `/hermes/v1/*` | `127.0.0.1:8642` | Agent API (OpenAI 兼容) |
| `/v1/chat/completions` | `127.0.0.1:3000` | OpenAI 格式代理 |
| `/v1/messages` | `127.0.0.1:3000` | Anthropic 格式代理 |
| `/chat` | 静态文件 | chat.html |
| `/health` | `127.0.0.1:3000` | 健康检查 |

**端口范围**: 9119-9200（共 82 个端口）

**限速**: `/api/admin/login` 5r/m，`/api/` 30r/m

**超时**: WS/SSE `proxy_read_timeout 600s`
