# 方案：现网 agent 运行环境打成基础镜像

> 状态：**待评审**（先写方案，不动手）
> 关联：`docs/k8s-migration-plan.md`（总迁移方案）、`k8s/README.md`（构建部署指南）

## 1. 背景与动机

当前 agent 镜像构建依赖 `service.sh` 从**现网** `/root/.hermes/hermes-agent` rsync 出 agent + venv + uv python（见 `service.sh:15` `AGENT_SRC="${HERMES_AGENT_SRC:-/root/.hermes/hermes-agent}"`）。这有两个痛点：

1. **构建机必须有现网 agent** —— 在 CI、别人的开发机、或重建过的机器上无法构建。
2. **每次构建都重新 rsync 1.7G** —— 慢，且依赖现网当时的状态（不稳定）。

替代思路（都否决了）：
- 「源码进 git + 构建时重建 venv」：源码 75MB 进 git 可接受，但 `uv sync` 重建 591 包 venv 慢、依赖网络、跨机器版本一致性难保证，放弃了 lift-and-shift 的"现网忠实副本"优势。
- 「从 NousResearch git clone + apply patches」：patches 针对特定 commit，版本对齐脆弱。

**本方案**：把现网 `/root/.hermes` 的**运行环境部分**一次性固化成一个基础镜像。以后这个镜像就是稳定的 agent 运行时，任何机器 `docker pull` 即可构建上层应用镜像。

## 2. 现网 `/root/.hermes` 分类（决定打包范围）

### 2.1 ★应进基础镜像（通用运行环境，只读共享）

| 内容 | 体积 | 说明 |
|---|---|---|
| `hermes-agent/`（含 venv 1.7G + 源码 94M，已打补丁） | 2.7G | agent 运行时本体；剔除 tests/website/node_modules/.git 后含 venv 约 1.8G |
| `skills/` | 68M | 全局技能（mock-interview、productivity 等） |
| `bin/` | 68M | agent 二进制/脚本 |
| `pairing/` | 16K | 配对信息 |
| `cache/` | 288K | 只读缓存（models_dev_cache 等） |
| `hooks/` | 4K | 钩子 |
| `models_dev_cache.json` / `ollama_cloud_models_cache.json` | 2.3M | 模型元数据缓存 |
| **合计** | **~2.85G** | |

### 2.2 ✗不进基础镜像（密钥 / 用户态 / 运行态 / 备份）

| 内容 | 体积 | 不进的原因 / 替代 |
|---|---|---|
| `auth.json` | 1.3K | 凭证池 → k8s **Secret** 挂载 |
| `config.yaml` | 16K | 含密钥/配置 → **ConfigMap**（脱敏后） |
| `.env` | 18K | 密钥 → **Secret** + `envFrom` |
| `state.db` / `state-snapshots/` | 65M+65M | agent 运行态 → PVC（或阶段二迁 PG） |
| `sessions/` | 123M | 会话历史 → PVC |
| `workspace/` | 2.1G | 工作区 → PVC |
| `backup/` | 221M | 备份 → 不进镜像 |
| `logs/` `cron/` `audio_cache/` `image_cache/` `memories/` `kanban.db` 等 | 小 | 运行态 → PVC |

## 3. 三种打包范围（待定）

### 方案 A：通用运行环境（**推荐**）
只打包 §2.1 的内容。基础镜像 = 纯净的 agent 运行时，不含任何用户态/密钥。
- **体积**：~3.0~3.5GB（agent venv 是大头，不可压缩）
- **用途**：多用户共用此基础镜像；用户数据靠 PVC，密钥靠 Secret
- **优点**：通用、可复用、符合"基础镜像"语义
- **这是真正意义上的"基础镜像"**

### 方案 B：含某用户运行态
连现网某个用户的 `state.db` / `sessions` / `workspace` 一起打包。
- **体积**：~5.5GB（多 2.3G 用户态）
- **用途**：完整复制现网某个用户的运行状态（一次性快照）
- **缺点**：不通用（含特定用户数据），不适合当共享基础镜像
- **适用场景**：调试、迁移单用户环境

### 方案 C：最小化（仅 agent，不含全局 skills/bin）
只打 `hermes-agent/` + uv python，skills/bin 等用 ConfigMap 或 initContainer 挂载。
- **体积**：~2.9GB
- **缺点**：skills 升级要改挂载，复杂度上升
- **不推荐**：lift-and-shift 阶段不值得

## 4. 推荐方案 A 的实施设计

### 4.1 一次性打包：现网 → 基础镜像

打包脚本（一次性运行，产出现网 agent 的基础镜像）：
```bash
# scripts/build-base-image.sh（待创建）
HERMES=/root/.hermes
# 只保留 §2.1 的内容，剔除密钥/用户态/备份/日志
docker build -t hermes-base:2026-06-26 -f - . <<'EOF'
FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx git ca-certificates curl patch procps sqlite3 && rm -rf /var/lib/apt/lists/*
# 只 COPY 运行环境（不含 auth.json/config.yaml/.env/state.db/sessions/workspace/backup/logs）
COPY hermes/hermes-agent     /root/.hermes/hermes-agent
COPY hermes/skills          /root/.hermes/skills
COPY hermes/bin             /root/.hermes/bin
COPY hermes/pairing         /root/.hermes/pairing
COPY hermes/cache           /root/.hermes/cache
COPY hermes/hooks           /root/.hermes/hooks
COPY hermes/models_dev_cache.json       /root/.hermes/
COPY hermes/ollama_cloud_models_cache.json /root/.hermes/
# uv python（venv 解释器，必须同路径）
COPY uv-python/cpython-3.11.15-linux-x86_64-gnu /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu
RUN ln -s /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu \
          /root/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu
EOF
```

打包前用 `rsync --exclude` 从现网取 §2.1 的内容（剔除密钥/用户态）。

### 4.2 上层应用镜像：FROM 基础镜像

应用层 Dockerfile（`docker/Dockerfile`，**改写**）：
```dockerfile
FROM hermes-base:2026-06-26          # ← 基础镜像（含 agent + venv + skills）
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
WORKDIR /opt/hermes-platform
COPY hermes_broker.py process_manager.py ws_pool.py api_server.py api_server_standalone.py ./
COPY chat.html favicon.ico ./
COPY patches ./patches
COPY scripts ./scripts
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/default.conf
COPY k8s/entrypoint.sh /entrypoint.sh
ENV PYTHONUNBUFFERED=1
EXPOSE 80 443
ENTRYPOINT ["/entrypoint.sh"]
```

**优势**：
- 应用镜像只含 broker 代码（~1MB），agent 在基础镜像里
- 改 broker 代码 → 只重建应用镜像（秒级），基础镜像不动
- 基础镜像版本固定（`hermes-base:2026-06-26`），agent 升级才重打

### 4.3 agent 升级流程

agent / skills 变更时，重跑 §4.1 打新基础镜像（如 `hermes-base:2026-07-01`），应用 Dockerfile 改 `FROM` tag。agent 升级不频繁，可接受。

## 5. 与当前方案对比

| | 当前（service.sh rsync） | 本方案（基础镜像 + 应用层） |
|---|---|---|
| agent 来源 | 每次构建从现网 rsync 1.7G | 一次打成基础镜像，以后 pull |
| 构建机要求 | 必须有现网 `/root/.hermes` | 只需 docker |
| 改 broker 代码重建 | 需重新 rsync agent（慢） | 只重建 ~1MB 应用层（秒级） |
| 源码进 git | 不需要 | 不需要 |
| 重建 venv | 不需要 | 不需要 |
| agent 版本追溯 | 依赖现网状态 | 基础镜像 tag 固定 |
| 镜像总体积 | 单镜像 2.96GB | 基础 ~3.2GB + 应用 ~1MB |

## 6. 待确认的决策点

1. **打包范围**：方案 A（通用，推荐）/ B（含用户态）/ C（最小化）—— 见 §3
2. **基础镜像存放**：推到内部 registry（`qx-images.tencentcloudcr.com`）还是本地？需 registry 写权限
3. **uv python 是否也固化进基础镜像**：推荐是（venv 依赖它），§4.1 已含
4. **是否同时把 `skills/` `bin/` 进基础镜像**：推荐是（§2.1），否则要用 ConfigMap 挂载，复杂
5. **现有 `service.sh` 去留**：本方案落地后，`service.sh` 的 rsync 逻辑可保留作为"重打基础镜像"的备选，或废弃

## 7. 风险与注意

- **venv 可移植性**：基础镜像的 venv 是现网编译的，C 扩展依赖宿主 `.so`。`python:3.11-slim` 基础上已验证 9 个关键 C 扩展可 import（见之前验证记录），但完整 591 包未逐一验证，运行时若个别包 import 失败需补 `apt-get install libxxx`。
- **镜像体积**：基础镜像 ~3.2GB（venv 1.7G 不可压缩），拉取慢。可用 registry 预热 + 多节点缓存缓解。
- **uv python 路径耦合**：venv 的 symlink 写死 `/root/.local/share/uv/python/cpython-3.11.15-...`，基础镜像必须放同路径（§4.1 已处理）。
- **现网已打补丁**：基础镜像里的 agent 是**现网已应用补丁**的版本，`patches/` 仅作 provenance 留档，不重新 apply。

## 8. 下一步

确认 §6 决策点后：
1. 写 `scripts/build-base-image.sh`（现网 → 基础镜像）
2. 改写 `docker/Dockerfile` 为 `FROM hermes-base:<tag>` 的应用层
3. 实际打一次基础镜像 + 构建应用镜像，验证 agent 可起（复用之前的验证方法：`hermes_cli.main --help`、C 扩展 import）
4. 更新 `k8s/README.md` 的构建说明
