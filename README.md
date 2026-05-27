# Hermes Multi-User Chat Platform

将 [Hermes Agent](https://github.com/nousresearch/hermes-agent) 包装为多用户服务，每个用户分配独立的 Hermes 进程，通过 Web UI 进行对话。

## 架构

```
                        ┌─────────────────────┐
                        │   Nginx (443/HTTPS)  │
                        └──────┬──────────────┘
                               │
              ┌────────────────┼────────────────┐
              │                │                │
     /auth/*  │       /broker/*│         /hermes/ws/{port}
              │                │                │
     ┌────────▼───────┐       │                │
     │ Process Broker │       │                │
     │  (:8080)       │       │                │
     └───────┬────────┘       │                │
             │ 分配进程        │                │
    ┌────────▼────────▼───────▼────────────────┐
    │   Hermes Dashboard x N  (ports 9119-9200) │
    │   WS JSON-RPC + HTTP REST API             │
    └───────────────────────────────────────────┘
```

- **Process Broker** (`hermes_broker.py`): 按用户分配 Hermes dashboard 进程，维护预热池，空闲超时自动回收
- **Chat UI** (`chat.html`): 单页应用，连接 Broker 获取进程后直接与 Hermes 通信
- **Nginx**: 反向代理，路由 WS/HTTP 请求到对应端口

## 功能

- **GitHub OAuth 登录**: 支持 GitHub OAuth 授权登录（可选），未配置时回退到用户名模式
- **多用户隔离**: 每个用户独立 Hermes 进程
- **多会话管理**: 新建、切换、删除会话
- **会话持久化**: 通过 sessionKey 恢复历史上下文
- **流式响应**: WS 事件驱动，支持多会话并行流式
- **Markdown 渲染**: 代码高亮 + 复制按钮，流式输出实时渲染
- **文件上传**: 上传文件到 Hermes 工作目录，结合对话使用
- **定时任务**: 用户友好的频率设置（每 N 分钟/每小时/每天/每周/自定义 cron）
- **自定义模型**: 支持添加自定义模型提供商（OpenAI 兼容）
- **技能管理**: 搜索、安装、启用/禁用 Hermes 技能
- **HTTP API 集成**: 服务端加载会话列表、历史消息、HTTP 删除
- **心跳保活 + 断线自动重连**（指数退避）
- **预热池**: 减少冷启动延迟

## 快速开始

### 依赖

- Python 3.10+
- Hermes Agent 已安装（`/root/.hermes/hermes-agent/`）
- Nginx（可选，生产环境）
- Python 包: `fastapi`, `uvicorn`, `pyjwt`, `aiohttp`, `requests`

### 配置 GitHub OAuth（可选）

在 GitHub → Settings → Developer settings → OAuth Apps → New OAuth App：
- Application name: `Hermes Chat`
- Homepage URL: `https://your-domain.com`
- Authorization callback URL: `https://your-domain.com/auth/callback`

记下 Client ID 和 Client Secret。

### 启动 Broker

```bash
# 方式一：GitHub OAuth 模式
export GITHUB_CLIENT_ID=your_client_id
export GITHUB_CLIENT_SECRET=your_client_secret
export HERMES_NGINX_DOMAIN=your-domain.com  # 用于生成公网 URL
python3 hermes_broker.py

# 方式二：用户名模式（无需 OAuth）
python3 hermes_broker.py
```

Broker 启动后监听 `0.0.0.0:8080`，预热池自动填充。

### 启动 Nginx

```bash
sudo cp nginx/hermes.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 访问

- **OAuth 模式**: 浏览器打开 `https://your-domain.com/chat`，点击 "Login with GitHub"
- **用户名模式**: 浏览器打开 `https://your-domain.com/chat`，输入用户名即可

## API 文档

完整 API 文档见 [docs/api.md](docs/api.md)。

### Broker API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/broker/sessions` | 分配进程（OAuth cookie 或 `{user_id}`） |
| GET | `/broker/sessions/{user_id}` | 查询状态 |
| DELETE | `/broker/sessions/{user_id}` | 回收进程 |
| POST | `/broker/upload` | 上传文件（multipart/form-data） |
| GET | `/broker/health` | 健康检查 |
| GET | `/broker/stats` | 统计信息 |

### GitHub OAuth API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/auth/github` | 重定向到 GitHub 授权 |
| GET | `/auth/callback` | OAuth 回调，签发 JWT cookie |
| GET | `/auth/user` | 查询当前登录用户 |
| POST | `/auth/logout` | 退出登录 |

### Hermes 进程 API

每个 Hermes 进程暴露两类接口：

**WebSocket JSON-RPC**（`/api/ws?token={token}`）

| 方法 | 说明 |
|------|------|
| `session.create` | 创建新会话 |
| `session.resume` | 恢复历史会话（参数为 sessionKey） |
| `session.close` | 关闭活跃会话 |
| `session.list` | 列出会话 |
| `prompt.submit` | 发送消息，触发流式响应 |
| `image.attach` | 附加图片文件 |
| `input.detect_drop` | 注册拖放文件 |
| `skills.manage` | 技能搜索/安装/浏览 |

**HTTP REST**（`Authorization: Bearer {token}`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sessions` | 列出所有会话 |
| GET | `/api/sessions/{key}` | 会话详情 |
| GET | `/api/sessions/{key}/messages` | 消息历史 |
| DELETE | `/api/sessions/{key}` | 删除会话 |
| GET | `/api/status` | 服务状态（公开） |
| GET/PUT | `/api/skills` / `/api/skills/toggle` | 技能管理 |

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `GITHUB_CLIENT_ID` | 空 | GitHub OAuth Client ID |
| `GITHUB_CLIENT_SECRET` | 空 | GitHub OAuth Client Secret |
| `HERMES_NGINX_DOMAIN` | 空 | 公网域名，用于生成 WS/HTTP URL |
| `HERMES_PUBLIC_HOST` | `127.0.0.1` | 无域名时的回退地址 |
| `SESSIONS_ROOT` | `/tmp/hermes_sessions` | 进程工作目录 |
| `BASE_PORT` | `9119` | 端口范围起始 |
| `MAX_PORT` | `9200` | 端口范围结束 |
| `IDLE_TIMEOUT` | `1800` | 空闲超时（秒） |
| `WARM_POOL_SIZE` | `3` | 预热池大小 |

## 另一种模式：多租户 Platform API

除 Broker 模式外，还提供完整的多租户 API（`api_server.py`），支持 PostgreSQL + Redis，按 API Key 隔离租户，配额管理和频率限制。

开发/测试可使用无依赖版本：

```bash
python3 api_server_standalone.py
```

预置测试密钥：`sk-alp-haaa0001`（20 sessions）、`sk-bet-hbbb0002`（5 sessions）。

## License

MIT
