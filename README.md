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
     /broker/*          /hermes/ws/{port}   /hermes/dash/{port}
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

- 多用户隔离：每个用户独立 Hermes 进程
- 多会话管理：新建、切换、删除会话
- 会话持久化：通过 sessionKey 恢复历史上下文
- 流式响应：WS 事件驱动，支持多会话并行流式
- HTTP API 集成：服务端加载会话列表、历史消息、HTTP 删除
- 心跳保活 + 断线自动重连（指数退避）
- 预热池：减少冷启动延迟

## 快速开始

### 依赖

- Python 3.10+
- Hermes Agent 已安装（`/root/.hermes/hermes-agent/`）
- Nginx（可选，生产环境）

### 启动 Broker

```bash
# 设置环境变量
export HERMES_NGINX_DOMAIN=your-domain.com  # 可选，用于生成公网 URL

# 启动
python3 hermes_broker.py
```

Broker 启动后监听 `0.0.0.0:8080`，预热池自动填充。

### 启动 Nginx

```bash
sudo cp nginx/hermes.conf /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

### 访问

浏览器打开 `https://your-domain.com/chat`，输入用户名即可开始对话。

## Hermes 进程 API

每个 Hermes 进程暴露两类接口：

### WebSocket JSON-RPC（`/api/ws?token={token}`）

| 方法 | 说明 |
|------|------|
| `session.create` | 创建新会话 |
| `session.resume` | 恢复历史会话（参数为 sessionKey） |
| `session.close` | 关闭活跃会话，释放资源 |
| `session.status` | 查询会话状态，获取 sessionKey |
| `prompt.submit` | 发送消息，触发流式响应 |

流式事件：`message.start` → `message.delta` → `message.complete`，每个事件携带 `session_id` 用于多会话路由。

### HTTP REST（`Authorization: Bearer {token}`）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sessions` | 列出所有会话 |
| GET | `/api/sessions/{key}` | 会话详情 |
| GET | `/api/sessions/{key}/messages` | 消息历史 |
| DELETE | `/api/sessions/{key}` | 删除会话 |
| GET | `/api/status` | 服务状态（公开） |

### Broker API

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/broker/sessions` | 分配进程 `{user_id}` |
| GET | `/broker/sessions/{user_id}` | 查询状态 |
| DELETE | `/broker/sessions/{user_id}` | 回收进程 |
| GET | `/broker/health` | 健康检查 |

## 配置

### 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
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
