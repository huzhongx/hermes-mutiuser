# Skill 管理 API 文档

**后端**: `hermes_broker.py` ｜ **基础路径**: `/api/skills/*` ｜ **认证**: JWT cookie 或 `Authorization: Bearer {ws_token}`

Hermes 支持两类技能，通过 symlink 机制区分：

| 类型 | 存储位置 | 来源 | 可删除 |
|------|----------|------|--------|
| 系统技能 | `/root/.hermes/skills/{name}` (symlink) | Broker 启动时自动同步 | 否 |
| 用户技能 | `{hermes_home}/skills/{name}` (真实目录) | 用户上传安装 | 是 |

---

## 1. POST /api/skills/upload

上传 zip 安装技能到用户目录。

### 请求

**Content-Type**: `multipart/form-data`

```bash
curl -X POST https://huzhongxiang.cloud/api/skills/upload \
  -H "Cookie: hermes_session=<jwt>" \
  -F "file=@my-skill.zip"
```

**Content-Type**: `application/json`（备选，base64 传文件）

```json
{
  "filename": "my-skill.zip",
  "data": "<base64-encoded-zip>"
}
```

### 处理流程

1. 接收 `.zip` 文件（仅支持 zip 格式）
2. 解压到临时目录，自动定位含 `SKILL.md` 的目录：
   - 单目录 zip → 该目录
   - 根目录直接含 `SKILL.md` → 根目录
   - 多目录 zip → 查找第一个含 `SKILL.md` 的子目录
3. 复制到 `{hermes_home}/skills/{skill_name}/`
4. 通过 WS 发送 `skills.reload` 触发 Hermes 进程热加载

### 响应 `200`

```json
{
  "status": "installed",
  "name": "my-skill"
}
```

### 错误

| 状态码 | 说明 |
|--------|------|
| 400 | 非 zip 文件 / 缺少 SKILL.md / 空压缩包 |
| 415 | Content-Type 不是 multipart/form-data 或 application/json |
| 401 | 未认证 |

---

## 2. GET /api/skills/list

列出所有技能，自动同步系统技能，标记来源类型。

### 请求

```bash
curl https://huzhongxiang.cloud/api/skills/list \
  -H "Cookie: hermes_session=<jwt>"
```

### 处理流程

1. **自动同步系统技能**：对比全局 `/root/.hermes/skills/` 与用户 `skills/` 目录
   - 缺失的系统技能 → 自动创建 symlink
   - 用户目录下被覆盖的系统技能（非 symlink）→ 恢复为 symlink（原用户副本删除）
2. 如果有新增或迁移，通过 WS 发送 `skills.reload`
3. 从 Hermes 进程 `GET /api/skills` 获取技能列表
4. 标记每个技能的来源

### 响应 `200`

```json
[
  {
    "name": "hina-telemule-skill",
    "user_installed": false,
    "...": "其他字段由 Hermes 进程返回"
  },
  {
    "name": "my-custom-skill",
    "user_installed": true,
    "...": "其他字段由 Hermes 进程返回"
  }
]
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | 技能目录名 |
| `user_installed` | boolean | `false` = 系统 symlink，`true` = 用户安装 |
| (其他) | - | 由 Hermes 进程 `/api/skills` 接口返回的原有字段 |

---

## 3. DELETE /api/skills/{skill_name}

删除用户安装的技能。系统技能（symlink）不可删除。

### 请求

```bash
curl -X DELETE https://huzhongxiang.cloud/api/skills/my-custom-skill \
  -H "Cookie: hermes_session=<jwt>"
```

### 处理流程

1. 检查技能路径：不存在返回 404，symlink 返回 400
2. 删除用户技能目录（`shutil.rmtree`）
3. 通过 WS 发送 `skills.reload` 触发热加载

### 响应 `200`

```json
{
  "status": "deleted",
  "name": "my-custom-skill"
}
```

### 错误

| 状态码 | 说明 |
|--------|------|
| 404 | 技能不存在 |
| 400 | 系统技能不可删除（symlink） |
| 401 | 未认证 |

---

## 4. 进程启动时自动同步（内部）

Broker 在 `_spawn()` 阶段自动执行：

1. 遍历 `/root/.hermes/skills/` 下所有子目录
2. 为每个系统技能在用户 `{hermes_home}/skills/` 创建 symlink
3. 同时 symlink `.skills_prompt_snapshot.json` 快照文件

```
/root/.hermes/skills/
  ├── hina-telemule-skill/
  ├── job-posting-assistant/
  └── ...
        ↓ symlink
/tmp/hermes_sessions/{user}/hermes_home/
  ├── skills/
  │   ├── hina-telemule-skill → /root/.hermes/skills/hina-telemule-skill
  │   └── job-posting-assistant → /root/.hermes/skills/job-posting-assistant
  └── .skills_prompt_snapshot.json → /root/.hermes/.skills_prompt_snapshot.json
```

---

## 架构要点

| 项目 | 说明 |
|------|------|
| 全局技能源 | `/root/.hermes/skills/`，所有用户共享 |
| 用户技能目录 | `{hermes_home}/skills/`，进程独立隔离 |
| 热加载机制 | 上传/删除/同步后通过 WS JSON-RPC 发送 `skills.reload` |
| 冲突策略 | 同名时系统技能优先，用户副本被替换为 symlink |
| 技能校验 | zip 内必须包含 `SKILL.md`，否则拒绝安装 |
