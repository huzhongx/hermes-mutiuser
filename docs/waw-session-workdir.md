# waw 前端接入:per-hermes-session 工作目录

## 背景

工作目录改为两级结构,**按 hermes session 隔离产物**:

```
/tmp/hermes_sessions/{user_id}/
  ├─ {hermes_session_id}/    ← 每个 hermes session 的产物目录(简历、报告、PDF…)
  ├─ hermes_home/            ← 用户级配置/技能/记忆(稳定,跨 session)
  └─ .broker-logs/           ← 进程日志(勿动)
```

broker 启动 hermes 进程时 `cwd = {user_id}/`(用户根)。**每个 hermes session 的产物子目录,由前端在创建 session 后建立并切换**。

## 为什么需要前端两步走

hermes session ID 是 `session.create` 的**返回值**(服务端生成,8 位 hex),不能在创建时作为入参传入。所以必须:**先 create 拿到 sid → 再用 sid 建目录并切换 cwd**。

---

## 接入流程(3 步)

### 前置:已通过 `POST /broker/sessions` 拿到进程

broker 返回(关键字段):
```json
{
  "user_id": "waw:cmq0q04ye...:workspace:talent",
  "port": 9123,
  "ws_token": "W4l3H8zNBj...",
  "ws_url": "wss://<domain>/hermes/ws/9123/api/ws?token=<ws_token>",
  "http_url": "https://<domain>/hermes/dash/9123"
}
```

- WS 连接:`ws_url`(用于 session.create / session.cwd.set)
- HTTP:`http_url`(用于 mkdir)

### 步骤 1:`session.create` —— 创建 hermes session,拿到 sid

```jsonc
// WS: wss://<domain>/hermes/ws/{port}/api/ws?token=<ws_token>
// 发送:
{ "jsonrpc": "2.0", "id": 1, "method": "session.create", "params": { "cwd": "{user_root}" } }
```

- `cwd` 传 **用户根** `{user_root}`(=`/tmp/hermes_sessions/{user_id}`,已存在)。
  - ⚠️ 不要传 `{user_root}/{sid}` —— 此时 sid 还不存在,且 `session.create` 要求 cwd 目录已存在。
- `user_root` 可从 `user_id` 拼出:`/tmp/hermes_sessions/` + `user_id`。

返回(关键字段):
```json
{ "jsonrpc": "2.0", "id": 1, "result": { "session_id": "487d3edc", ... } }
```
- **`result.session_id`** 就是 hermes session ID(8 位,如 `487d3edc`),记为 `sid`。

### 步骤 2:建产物目录 `{user_root}/{sid}/`

```http
POST {http_url}/api/files/mkdir
Content-Type: application/json
Authorization: Bearer <ws_token>

{ "path": "{user_root}/{sid}" }
```

示例:
```http
POST https://<domain>/hermes/dash/9123/api/files/mkdir
{ "path": "/tmp/hermes_sessions/waw:cmq0q04ye...:workspace:talent/487d3edc" }
```

返回:
```json
{ "ok": true, "entry": { "name": "487d3edc", "path": "...", "is_directory": true, ... } }
```

- 该接口建目录(`mkdir -p`),已存在不报错。
- 路径必须用**绝对路径**。

### 步骤 3:`session.cwd.set` —— 把该 session 的工作目录切到 `{user_root}/{sid}/`

```jsonc
// WS: wss://<domain>/hermes/ws/{port}/api/ws?token=<ws_token>
{ "jsonrpc": "2.0", "id": 2, "method": "session.cwd.set", "params": {
    "session_id": "{sid}",
    "cwd": "{user_root}/{sid}"
}}
```

返回:
```json
{ "jsonrpc": "2.0", "id": 2, "result": { "cwd": "/tmp/hermes_sessions/.../487d3edc", "branch": "..." } }
```

完成后,该 hermes session 内 agent 执行的所有命令、写文件、生成产物,都会落在 `{user_root}/{sid}/`。

---

## 完整时序图

```
前端                          broker(:8080)              hermes 进程(:9xxx)
 │                              │                           │
 │ POST /broker/sessions        │                           │
 │ ├─ user_id ─────────────────►│ acquire → spawn(cwd=user_root)
 │ ◄─── port, ws_token ────────┤                           │
 │                              │                           │
 │ WS session.create(cwd=user_root) ─────────────────────►│  sid = uuid4().hex[:8]
 │ ◄─── result.session_id ───────────────────────────────┤  (建 session)
 │                              │                           │
 │ POST {http_url}/api/files/mkdir {path: user_root/sid} ►│  mkdir user_root/sid
 │ ◄─── {ok:true} ───────────────────────────────────────┤
 │                              │                           │
 │ WS session.cwd.set(sid, user_root/sid) ──────────────►│  切 cwd
 │ ◄─── result.cwd ──────────────────────────────────────┤
 │                              │                           │
 │ WS prompt.submit(sid, ...) ──────────────────────────►│  产物 → user_root/sid/
```

## 文件操作:走 hermes 进程原生接口

所有文件操作(下载/上传/建目录)走 **hermes 进程原生接口**,经 nginx `/hermes/dash/{port}/` 直连该用户的 hermes 进程。broker 已给每个进程注入 `HERMES_DASHBOARD_FILES_ROOT={user_root}`,把 hermes 的 managed-files root **锁定到该用户工作区**——越权访问(如 `/etc/passwd`、broker 的 `.env`)会被 `_path_is_under()` 拦截返回 403。

### 基础 URL 与鉴权

```http
https://<domain>/hermes/dash/{port}/api/files/...
```

- `{port}`:`POST /broker/sessions` 返回的 `port`(9119-9200)
- 鉴权(任选其一):
  - `Authorization: Bearer <ws_token>`
  - `X-Hermes-Session-Token: <ws_token>` header
  - `?token=<ws_token>` query(适合浏览器直接打开的下载链接)
- `path` 参数一律用**绝对路径**(在 `{user_root}` 下)

### 下载产物

```http
GET https://<domain>/hermes/dash/{port}/api/files/download?path={user_root}/{sid}/{filename}&token=<ws_token>
```

示例:
```http
GET /hermes/dash/9123/api/files/download?path=/tmp/hermes_sessions/waw:cmq0q04ye...:workspace:talent/487d3edc/talent_360_report.html&token=W4l3H8...
```

- `path` 必须在 `{user_root}` 下(否则 403)
- 返回 `attachment`(图片类自动 inline)
- 大小上限 100MB(`_MANAGED_FILE_MAX_BYTES`)

### 建目录(步骤 2 用的就是这个)

```http
POST https://<domain>/hermes/dash/{port}/api/files/mkdir
Authorization: Bearer <ws_token>
Content-Type: application/json

{ "path": "{user_root}/{sid}" }
```

### 上传文件

```http
POST https://<domain>/hermes/dash/{port}/api/files/upload
Authorization: Bearer <ws_token>
Content-Type: multipart/form-data

file: <binary>
```

### 不要再用 broker 的文件接口

旧的 `GET /api/files/download/{path}`(走 broker :8080)**已废弃**,前端应迁移到上面的 hermes 原生接口。broker 文件接口仅作过渡保留。

## 注意事项 / 约束

1. **顺序必须**:create → mkdir → cwd.set,且要在首次 `prompt.submit` **之前**完成。
   - 否则 agent 会把产物写到 user_root 根(不符合预期)。
2. `session.create` 和 `session.cwd.set` 的 `cwd` 都要求**目录已存在**,所以 mkdir 不能省。
3. 一个用户**一个活跃进程**,但可以有**多个 hermes session**(每个 session 独立目录)。切换 session 时重复步骤 1-3(新 sid → 新目录)。
4. `hermes_home/` 是用户级共享(配置/技能/state.db),**不要**当产物目录。
5. **文件操作必须带 port**(每个用户进程端口不同);`port` 从 `/broker/sessions` 返回值取。

## 鉴权

- `POST /broker/sessions`:JWT cookie(`hermes_session`)优先,fallback `X-Hermes-Session-Token` header / `X-User-ID` / body.user_id。
- hermes 原生文件接口 + WS:`Authorization: Bearer <ws_token>` 或 `X-Hermes-Session-Token` header(或下载链接的 `?token=`)。

## 验证清单(接入后自测)

- [ ] `session.create` 返回的 `session_id` 非空(8 位 hex)
- [ ] `mkdir`(hermes 原生)返回 `ok:true`,目录实际存在
- [ ] `session.cwd.set` 返回的 `cwd` = `{user_root}/{sid}`
- [ ] 让 agent 写个文件(如 `open('test.txt','w')`),文件落在 `{user_root}/{sid}/test.txt`
- [ ] 下载 `{user_root}/{sid}/test.txt` 经 hermes 原生接口(`/hermes/dash/{port}/api/files/download`)返回 200
- [ ] 越权下载 `/etc/passwd` 返回 **403**(验证 locked_root 生效)
