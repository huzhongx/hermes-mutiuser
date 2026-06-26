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
     /auth/*  │       /api/*   │         /hermes/ws/{port}
              │   (Web Proxy)  │         (internal only)
              │                │                │
     ┌────────▼───────┐       │                │
     │ Process Broker │       │                │
     │  (:8080)       ├───────┘                │
     │ + Web Proxy    │                        │
     └───────┬────────┘                        │
             │ 分配进程 + 代理转发               │
    ┌────────▼────────▼────────────────────────┐
    │   Hermes Dashboard x N  (ports 9119-9200) │
    │   WS JSON-RPC + HTTP REST API             │
    └───────────────────────────────────────────┘
```

- **Process Broker + Web Proxy** (`hermes_broker.py`): 按用户分配 Hermes dashboard 进程，维护预热池，空闲超时自动回收。同时作为对外唯一安全入口，代理所有 WS/HTTP 请求，隐藏内部端口和 token
- **Chat UI** (`chat.html`): 单页应用，通过 `/api/*` 代理层与 Hermes 通信，支持交互式面板（澄清、审批、sudo、密钥）
- **Nginx**: 反向代理，路由 WS/HTTP 请求到对应端口

## 功能

- **GitHub OAuth 登录**: 支持 GitHub OAuth 授权登录（可选），未配置时回退到用户名模式；JWT secret 持久化到磁盘，broker 重启后已登录用户不会被强制登出
- **多用户隔离**: 每个用户独立 Hermes 进程
- **多会话管理**: 新建、切换、删除会话
- **会话持久化**: 通过 sessionKey 恢复历史上下文
- **流式响应**: WS 事件驱动，支持多会话并行流式
- **Markdown 渲染**: 代码高亮 + 复制按钮，流式输出实时渲染
- **文件上传 + 文件面板**: 上传文件到 Hermes 工作目录（最大 50MB），并通过文件面板浏览、下载、删除工作目录中的文件
- **交互式面板**: Agent 可发起澄清问题（clarify）、命令审批（approval）、sudo 密码、密钥/凭证请求，用户通过弹窗交互回复
- **定时任务**: 用户友好的频率设置（每 N 分钟/每小时/每天/每周/自定义 cron）
- **自定义模型**: 支持添加自定义模型提供商（OpenAI 兼容）
- **MCP 服务器管理**: UI 添加/移除 MCP 服务器，支持 MCP OAuth 远程授权——用户通过 nginx 反代完成 OAuth 流程，无需直连 Hermes 进程的 localhost
- **技能管理**: 搜索、安装、启用/禁用 Hermes 技能（系统/用户分类展示）；用户可在 `.skipped/{skill_name}` 中声明跳过特定系统技能
- **Web Proxy 安全层**: 对外唯一 `/api/*` 入口，隐藏内部端口和 token
- **HTTP API 集成**: 服务端加载会话列表、历史消息、HTTP 删除
- **在线 API 文档**: 访问 `/docs/api`、`/docs/api-mcp-oauth`、`/docs/api-skills` 查看渲染后的 HTML 文档
- **心跳保活 + 断线自动重连**（指数退避）
- **预热池**: 减少冷启动延迟
- **自动压缩**: 会话消息超过 80 条时自动压缩历史以节省 token
- **可靠性**: 单用户分配加锁（singleflight）防止并发请求重复 spawn 进程；分配时自动检测并清理已死亡的残留进程；空闲回收前检查 dashboard 端口是否仍有活跃 WS 连接，避免误杀进行中的会话

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

完整的 Markdown 文档见 [docs/api.md](docs/api.md)、[docs/api-mcp-oauth.md](docs/api-mcp-oauth.md)、[docs/api-skills.md](docs/api-skills.md)。Broker 还提供渲染后的在线 HTML 文档：部署后浏览器访问 `/docs/api`、`/docs/api-mcp-oauth`、`/docs/api-skills`。

> **对外入口**：所有外部请求应通过 Web Proxy 层的 `/api/*`（由 broker 在 `:8080` 提供，nginx 反代），它会注入正确的内部端口和 token，对外隐藏 Hermes dashboard 的真实地址。`/broker/*` 与 `/auth/*` 主要用于登录和进程管理。

### Broker API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/broker/sessions` | 分配进程（OAuth cookie 或 `{user_id}`） |
| GET | `/broker/sessions/{user_id}` | 查询状态 |
| DELETE | `/broker/sessions/{user_id}` | 回收进程 |
| POST | `/broker/upload` | 上传文件（multipart/form-data） |
| GET | `/broker/files/{user_id}/{filename}` | 取回上传的文件 |
| POST | `/broker/reload` | 热重载 broker 代码（保留运行中的进程） |
| POST | `/broker/reload-mcp/{user_id}` | 重载指定用户的 MCP 服务器配置 |
| POST | `/broker/drain` | 进入优雅停机：停止接收新分配，等现有进程空闲后回收 |
| POST | `/broker/drain/cancel` | 取消优雅停机 |
| GET | `/broker/drain` | 查询优雅停机状态 |
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
| `session.compress` | 压缩历史消息 |
| `prompt.submit` | 发送消息，触发流式响应 |
| `clarify.respond` | 回答 Agent 澄清问题 |
| `approval.respond` | 命令审批（允许/拒绝） |
| `sudo.respond` | 提供 sudo 密码 |
| `secret.respond` | 提供密钥/凭证 |
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
| GET | `/api/files` / `/api/workdirs` | 列出工作目录与文件 |
| GET | `/api/files/download/{path}` | 下载文件（支持文件夹打包下载） |

### MCP OAuth 远程授权

MCP 服务器需要 OAuth 时（如 Notion、GitHub MCP），远程用户通过 nginx 反代完成授权，无需访问 Hermes 进程的 localhost。详情见 [docs/api-mcp-oauth.md](docs/api-mcp-oauth.md)。

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/mcp/servers/{name}/oauth/start` | 发起 OAuth 流程，返回授权 URL |
| GET | `/api/mcp/servers/{name}/oauth/status` | 轮询授权状态（awaiting_callback → completed） |
| GET | `/hermes/mcp-oauth-cb/{port}/callback` | OAuth provider 回调入口（nginx 反代到内部端口） |

支持两种触发场景：用户在 MCP 设置面板点击 **Connect** 主动授权；或 Agent 调用 MCP 工具收到 401 时自动触发，前端通过 WS `tool.complete` 事件中的 `needs_reauth`/`oauth_url` 渲染授权提示。

## 配置

### Hermes 用户配置（config.yaml）

每个用户的 Hermes 配置文件位于 `{SESSIONS_ROOT}/{user_id}/hermes_home/config.yaml`。以下是关键配置项：

```yaml
# 模型设置
model:
  default: glm-5.1                    # 默认模型
  provider: custom:openclaw-router    # 提供商
  base_url: ''
  context_length: 200000              # 上下文窗口大小（tokens），避免自动探测失败

# 自定义提供商
custom_providers:
  - name: openclaw-router
    base_url: https://your-domain.com/v1
    model: glm-5.1
    api_key: your-api-key

# 代码执行沙箱
code_execution:
  mode: project          # project=使用项目目录和venv, strict=隔离临时目录
  timeout: 600           # 执行超时（秒），轮询类脚本需要设大（默认300不够）
  max_tool_calls: 50     # 单次脚本最大工具调用次数
```

> **注意事项**：
> - `model.context_length` 建议显式设置，否则每次启动会探测 API 并 fallback 到 256K
> - `code_execution.timeout` 如果用户脚本中有轮询等待逻辑（如等待 API 状态变化），300s 可能不够用，建议调大到 600s
> - 修改配置后需要通过 broker 释放并重新分配 session 才能生效：
>   ```bash
>   curl -X DELETE http://127.0.0.1:8080/broker/sessions/{user_id}
>   curl -X POST http://127.0.0.1:8080/broker/sessions -H "Content-Type: application/json" -d '{"user_id":"{user_id}"}'
>   ```

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
| `MAX_SESSIONS` | `80` | 同时运行的最大用户进程数 |
| `IDLE_TIMEOUT` | `1800` | 空闲超时（秒） |
| `WARM_POOL_SIZE` | `3` | 预热池大小 |

> 这些变量可在项目根目录的 `.env` 中配置，broker 启动时自动读取。

### hermes-agent 补丁（patches/）

由于 hermes-agent 以安装目录形式运行（`/root/.hermes/hermes-agent/`），无法直接修改源码，因此所有对 hermes-agent 的改动都以补丁形式保存在仓库的 [patches/](patches/) 目录，便于版本管理、复现和随 GitHub 提交传播。当前包含的补丁涵盖：

- **MCP OAuth**：远程回调服务器、HTTP 端点、OAuth manager（含 Protected Resource Metadata 预发现，兼容 Notion 等服务器）
- **MCP 工具可靠性**：连接超时（每服务器 60s→10s）、401 自动重新授权、嵌套错误信息提取、业务错误不熔断、通用错误不触发 reauth、凭证池提供商不匹配修复
- **MCP CLI 修复**：`--env` 多 flag 解析、`mcp list` 崩溃修复
- **压缩**：历史压缩在日志轮转前 flush，避免丢失
- **辅助客户端**：OpenAI 环境变量 fallback、TUI 启动时 `custom` provider 路由修复
- **技能**：`skill_view` TTL 缓存（修复 threading 导入）
- **其他**：符号链接环检测

应用/更新补丁后，需要释放并重新分配受影响用户的 session 才能让 Hermes 进程加载新代码。

## 另一种模式：多租户 Platform API

除 Broker 模式外，还提供完整的多租户 API（`api_server.py`），支持 PostgreSQL + Redis，按 API Key 隔离租户，配额管理和频率限制。

开发/测试可使用无依赖版本：

```bash
python3 api_server_standalone.py
```

预置测试密钥：`sk-alp-haaa0001`（20 sessions）、`sk-bet-hbbb0002`（5 sessions）。

## License

MIT
