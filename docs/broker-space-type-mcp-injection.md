# Broker 按空间类型注入技能 MCP

## 背景

新用户 spawn 时,broker 从全局 config 复制(`mcp_servers: {}` 为空),waw 平台异步注入 `waw` MCP。但 talent 空间需要的技能 MCP(mock-interview / position-recommender / resume-intake / resume_optimizer)**不会自动获得** —— 之前靠运维手动给每个 talent 用户注册,新 talent 用户仍要手动操作。

本次让 broker 在 spawn 时**按空间类型自动注入**对应技能 MCP,新 talent 用户开箱即用。

## 机制

### 配置文件 `/root/.hermes/skills_space_mcp.json`

镜像 `skills_space_blacklist.json` 的设计(mtime 缓存 + 热加载)。格式:
```json
{
  "talent": {
    "mock-interview": { "command": "...", "args": [...], "env": {...} },
    "position-recommender": { ... },
    "resume-intake": { ... },
    "resume_optimizer": { ... }
  }
}
```
- key = 空间类型(从 `waw:<cuid>:workspace:<type>` 解析),value = `{mcp_name: mcp_config}`
- 只有 talent 配了 4 个;hiring/expert 不在文件 = 不注入
- 运维改文件后 mtime 变化,**下次 spawn 即生效**(无需重启 broker)

### broker 改动(`patches/broker-space-type-mcp-injection.patch`)

新增模块级(紧邻空间黑名单机制):
- `_SPACE_MCP_FILE` / `_SPACE_MCP_CACHE` —— 配置路径 + mtime 缓存
- `_load_space_mcp()` —— 热加载空间→MCP 映射
- `_space_mcp_for(user_id)` —— 返回该用户空间对应的 MCP dict(复用 `_parse_space_type`,非 waw 用户返回 `{}`)
- `_atomic_write_yaml(path, data)` —— 原子写 YAML(tempfile + 验证 + os.replace,避免 open("w") 截断事故)

`_spawn` 注入点(config 复制段之后):
- **仅首次创建** config 时(`not dst.exists()` 已由复制逻辑保证)
- 读 config → 补缺失的 MCP → 原子写回
- 仅注入 config 里没有的(`if mcp_name not in mcp`),不覆盖已有

### 关键设计(已确认)
- **仅首次注入**:已有 config 的用户不被覆盖(用户/waw 后续修改保留)
- **独立 JSON 文件**:与空间黑名单并列,职责清晰,热加载
- **空间过滤**:hiring/expert/普通用户不在配置文件 = 不注入,天然过滤

### waw 凭证顺序问题(无需特殊处理)
3 个技能 MCP(mock-interview/position-recommender/resume-intake)用 `credential-bootstrap.py` 包装,运行时(MCP 进程启动时)去 waw MCP 拿凭证。broker spawn 时只写 config(注入 MCP 配置),不依赖 waw 已注入 —— 等 waw 平台异步注入 waw MCP 后,MCP 进程启动时 credential-bootstrap 自然能拿到凭证。**注入顺序无关**。

## 验证(2026-07-06 已测)

- **新 talent 用户 spawn**:自动注入 4 个 MCP ✅(日志 `Injected 4 space MCP(s)`)
- **新 hiring 用户 spawn**:不注入(mcp_servers 空)✅
- **新 expert 用户**:不注入 ✅
- **普通/测试用户(无 workspace)**:不注入 ✅
- **已有 config 的用户**:不被覆盖(mtime 不变)✅
- **热加载**:mtime 缓存机制(单元测试验证)

## 运维操作

### 给某空间加技能 MCP
编辑 `/root/.hermes/skills_space_mcp.json`,加 `{<space>: {<mcp_name>: <config>}}`,下次 spawn 该空间用户即生效。

### 移除某空间的 MCP
从 JSON 删掉对应项。注意:**已 spawn 的用户 config 不会自动移除**(仅首次注入),需手动清理或等用户重新 spawn。

### 注意
- MCP 配置里的脚本路径(`__TARGET_SCRIPT`)依赖全局技能本体存在(`/root/.hermes/skills/<name>/scripts/mcp_server.py`)。删技能时要同步更新此配置文件。
- 此机制与空间黑名单(`skills_space_blacklist.json`)独立:黑名单管技能软链(用户能否看到技能),本机制管 MCP 配置(技能能否调用)。两者配合:talent 看得到技能 + 有 MCP → 可用;hiring 看不到技能(黑名单)+ 无 MCP → 不可用。
