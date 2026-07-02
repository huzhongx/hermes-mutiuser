# Dashboard 冷启动优化:禁用未使用的平台插件

## 问题

新用户调用 broker 分配 session 很慢,实测 **7.1s**(已有用户 fast-path 0ms)。

## 根因(实测定位)

每个新用户都是**冷启动**(暖池在 per-user HERMES_HOME 隔离下被禁用,`_warm_maintainer` 是空操作)。
冷启动 7s 全部花在 **dashboard 进程启动到就绪**(broker 日志:`启动 dashboard` → `就绪` 间隔 7.04s)。

`importtime` 量化构成:

| 重型 import | 耗时 | 必需? |
|---|---|---|
| `lark_oapi`(飞书 SDK,经 `feishu-platform` 插件) | ~2.7s | ❌ 未连接飞书 |
| `microsoft_teams`(经 `teams-platform` 插件) | ~1.0s | ❌ 未连接 Teams |
| 其他 18 个平台插件(discord/slack/telegram/...)累计 | ~0.9s | ❌ gateway.platforms 为空 |
| `mcp` + `web_server` + 其他 | ~2.5s | ✅ 必需 |

**核心**:hermes-agent 的插件管理器(`hermes_cli/plugins.py::_discover_and_load_inner`)**无条件加载 `plugins/platforms/` 下全部 20 个 bundled 平台插件**,每个平台 adapter 在模块顶层 `import` 自己的重型 SDK(lark_oapi / microsoft_teams / ...)。当前部署 `gateway.platforms` 未连接任何平台,这 ~4.6s 纯属浪费。

bundled 的 backend/platform 插件**绕过 `plugins.enabled` allow-list 强制加载**(plugins.py:1325),但 `plugins.disabled` 优先级最高(plugins.py:1284,在 `_load_plugin` 即 import 之前 `continue` 跳过)。

## 方案(纯配置,零代码改动)

在 config 的 `plugins.disabled` 列出所有未使用的平台插件名。插件管理器加载时跳过 → 不 import 重型 SDK → 冷启动加速。

平台插件的 manifest `name` 是 `<platform>-platform`(如 `feishu-platform`),同时兼容裸名(`feishu`),两种都写进 disabled 兜底。

禁用清单(20 个平台):
```
dingtalk-platform discord-platform email-platform feishu-platform
google_chat-platform homeassistant-platform irc-platform line-platform
matrix-platform mattermost-platform ntfy-platform photon-platform
raft-platform simplex-platform slack-platform sms-platform
teams-platform telegram-platform wecom-platform whatsapp-platform
(+ 对应裸名)
```

## 落地(2026-07-02)

1. **全局 config** `/root/.hermes/config.yaml`:加 `plugins.disabled`(影响未来新用户 spawn 时的 config 拷贝)
2. **批量更新 88 个用户 config**(memory `project_config_propagation` 提醒:已有用户有快照副本,必须逐个更新;不能只改全局)
   - 14 个测试用户 config(test_*/alice/0 等)有**历史遗留 YAML 语法错误**,本来就 parse 不了,与本次无关,未动
   - 生产用户(talent/hiring/expert + 普通用户)全部成功更新

## 验证

| 场景 | 优化前 | 优化后 |
|---|---|---|
| dashboard 进程冷启动(纯) | 7.10s | 2.47s |
| talent 用户冷启动(真实 config) | ~7.1s | 2.66s |
| **新用户经 broker acquire(端到端)** | **7.12s** | **3.08s** |

`importtime` 确认:禁用后 `lark_oapi` / `microsoft_teams` / `feishu_platform` **0 次 import**(彻底拦住)。

## 风险评估

- gateway.platforms 当前**未连接任何平台**,禁用平台插件不影响 agent dashboard 核心功能
- config 里有 `FEISHU_HOME_CHANNEL`、discord/slack/telegram 等配置键,但这些:
  - 仅在 gateway 模块激活时才被读取(`gateway/config.py`),dashboard 进程不跑 gateway
  - 对应 env(FEISHU_APP_ID 等)在 broker 用户进程里未注入,不会触发平台激活
- 若将来要启用某平台(如飞书),从 `plugins.disabled` 删掉对应项重启即可

## 边界

剩余 ~2.5s 冷启动来自必需的 `mcp`/`web_server` import + Python 启动,无法通过禁用插件进一步压缩。
要再快需暖池复用(需 hermes 支持 HERMES_HOME hot-swap)或 Python 启动加速(frozen modules),改动较大。
