# Hermes Platform → Kubernetes 迁移改造方案

> 版本：v1 · 编制日期：2026-06-22
> 范围：当前运行在单机（`/opt/hermes-platform` + `/root/.hermes`）的 Hermes broker 与 per-user 进程体系，迁移到 Kubernetes。
> 目标：在不改变前端与 WS/REST 协议的前提下，把「单机进程 broker」模型迁到 k8s，先解决「下掉本机 + 跑通集群」，再演进到可水平扩展 / HA。

---

## 1. 现状盘点（迁移基线）

### 1.1 进程组件（来自 `/etc/nginx/sites-enabled/openclaw.conf` 的 upstream）

| 组件 | 监听 | 作用 | 是否容器化 |
|---|---|---|---|
| **broker** (`hermes_broker.py`) | `127.0.0.1:8080` | 每用户分配一个 Hermes dashboard 子进程；OAuth/上传/会话编排；进程注册表与端口分配**纯内存** | ❌ |
| **per-user Hermes 进程** | `9119–9200`（82 端口，由 broker 动态分配） | 真正的 agent dashboard，WS JSON-RPC + REST；含 `tui_gateway.slash_worker` 子进程 | ❌（broker 在 pod 内 `create_subprocess_exec` 起的） |
| **hermes_api** | `127.0.0.1:8642` | Agent API（OpenAI 兼容，`/hermes/v1/*`） | ❌（本咨询范围外，需独立评估） |
| **openclaw router** | `127.0.0.1:3000` | `/v1/chat/completions`、`/v1/messages`（OpenAI/Anthropic 兼容网关） | ❌（独立组件，见 `/root/.openclaw/`） |
| **nginx** | `:443`（`huzhongxiang.cloud`） | TLS 终结 + 动态端口路由 + Origin 伪造 | 宿主机 nginx |

> 本方案聚焦 **broker + per-user 进程**（用户问题核心）。`hermes_api`、`openclaw` 作为独立服务一并容器化，但其内部改造不在本文细化。

### 1.2 状态与数据（迁移最关键部分）

| 路径 | 内容 | 大小 | 持久性要求 |
|---|---|---|---|
| `SESSIONS_ROOT`（`/tmp/hermes_sessions`） | 每个 user_id 一个目录：`hermes_home/`（skills 软链 + `state.db` sqlite + config）+ 会话 work_dir + uploads + logs | **1.2 G**，84 个 `state.db` | **必须持久**（用户会话历史、上传文件） |
| `/root/.hermes/hermes-agent/` | Hermes Agent 运行时 + venv | **3.7 G** | 烤进镜像（只读） |
| `/root/.hermes/skills/` | 全局技能源（含本次新置的 `job-posting-assistant`） | 22 M | 镜像层 / 只读挂载 |
| `/root/.hermes/config.yaml` | 全局 agent 配置（各 user 拷贝副本） | 16 K | ConfigMap |
| `/root/.hermes/auth.json` | 全局凭证池 | <1 K | **Secret**（高敏） |
| `/root/.hermes/{cache,pairing,*.json 缓存}` | 共享只读缓存 | 小 | 镜像层 |
| `.jwt_secret`（仓库目录） | JWT 签名密钥，模块加载时生成/持久化（`hermes_broker.py:806`） | 64 B | **Secret**（必须脱离文件自生成） |
| `/root/.openclaw/.../ssl/*.{pem,key}` | TLS 证书 | — | TLS Secret / cert-manager |
| `patches/` | 8 个 hermes-agent 补丁（构建时应用） | 小 | 镜像构建输入 |

### 1.3 配置（`.env`，17 变量）

| 变量 | k8s 载体 |
|---|---|
| `POSTGRES_HOST/PORT/USER/PASSWORD/DB`、`REDIS_URL` | Secret（broker 当前**未用** PG/Redis，仅 `api_server` SaaS 模式用；阶段一可空挂） |
| `SESSIONS_ROOT`、`BASE_PORT=9119`、`MAX_PORT=9200`、`MAX_SESSIONS`、`IDLE_TIMEOUT=1800`、`WARM_POOL_SIZE=3`、`API_HOST/PORT`、`RATE_LIMIT_RPM/WINDOW` | ConfigMap |
| `GITHUB_CLIENT_ID/SECRET` | Secret |
| `HERMES_NGINX_DOMAIN` | ConfigMap（集群对外域名） |

### 1.4 关键代码事实

- broker 调用 agent：`_HERMES_PYTHON="/root/.hermes/hermes-agent/venv/bin/python"`、`_HERMES_MODULE="hermes_cli.main"`（`hermes_broker.py:54-55`）→ **镜像必须保持该路径**。
- 每用户 HERMES_HOME：`{work_root}/{user_id}/hermes_home/`，spawn 时软链全局 skills/、拷贝 config.yaml、软链 auth.json/.env/缓存（`hermes_broker.py:441-460`）→ **跨镜像层软链要用容器内绝对路径**。
- 优雅退出：`_flush_and_kill`（`:542`）flush state.db → SIGTERM 10s → SIGKILL → 释放端口 → 清 logs。
- JWT：`_load_or_create_jwt_secret()`（`:808`）从 `.jwt_secret` 文件读，缺失则生成并 `0600` 落盘 → **k8s 下必须改为从 Secret 读，禁止自生成**。

---

## 2. 核心矛盾（为什么不能直接搬）

当前是「**单机进程 broker**」：用户来 → broker 在本机 `create_subprocess_exec` 起一个绑定 9119–9200 某端口的长期进程 → nginx 按 `/hermes/ws/{port}/` 路由过去，且 **WS 必须把 Origin 伪造为 `http://127.0.0.1:{port}`**（Hermes 校验 Origin）。

这与 k8s 三处正面对撞：

1. **动态端口 + 动态子进程** —— k8s Service 不支持「运行时才确定的端口」，外部 Ingress 也无法按端口路由。
2. **WS Origin 伪造** —— 必须 in-pod 反代才能伪造 `127.0.0.1:{port}` Origin；外部 Ingress 做不到。
3. **broker 内存注册表** —— `_procs`/`_warm`/idle 计时全在进程内，pod 重启全丢；多副本则状态不一致。

结论：**不能简单「写 Dockerfile 搬上去」**，要先决定保留多少当前模型。

---

## 3. 总体策略：两阶段

| 阶段 | 模型 | 收益 | 代价 |
|---|---|---|---|
| **阶段一：lift-and-shift** | 单副本 StatefulSet，broker 在 pod 内继续起子进程，**in-pod nginx 保留动态端口路由** | 代码几乎不改，行为完全一致；下掉本机、跑通集群 | 单点、无水平扩展 |
| **阶段二：拆单点** | broker 注册表迁 Redis（复用 `api_server.py` 设计）；用户进程改「每用户一 pod」或「worker 池调度」；存储改 RWX/PG | 水平扩展、HA、强隔离 | 大改 broker 调度与存储层 |

**决策点（决定阶段二走哪条，当前不必定死，取决于并发量级与是否要 HA）：**
- 阶段二-A：每用户一个 pod（强隔离，调度复杂，冷启动慢）
- 阶段二-B：固定 worker 池，broker 把用户调度到池节点（折中）
- 阶段二存储：sqlite-over-RWX（不稳）/ state.db 迁 PG（最稳，要改 agent）/ per-user PVC（用户多时 PVC 爆炸）

> **建议先完成阶段一并稳定运行**，再据真实并发决定阶段二。阶段一已能验证风险最高的「存储 + 网络 + 镜像」三件事。

---

## 4. 阶段一详细方案（lift-and-shift）

### 4.1 目标拓扑

```
                ┌─────────────────────────────────────────────┐
   用户 ──HTTPS──►  Ingress (TLS, huzhongxiang.cloud)            │
                │     routes: /broker /auth /hermes/ws /hermes/dash │
                │            /hermes/v1 /v1/* /chat ...          │
                └──────────────────────┬──────────────────────┘
                                       ▼
        ┌────────────────────────────────────────────── StatefulSet (replicas=1) ──────────┐
        │  Pod                                                                            │
        │  ┌────────────┐   ┌───────────────────┐                                         │
        │  │  broker    │◄──│  nginx (sidecar)  │  ← 动态端口路由 + Origin 伪造仍在此处    │
        │  │ :8080      │   │  :80 / :443       │                                         │
        │  └─────┬──────┘   └────────┬──────────┘                                         │
        │        │ create_subprocess_exec (127.0.0.1:9119-9200)                            │
        │        ▼                   ▼                                                     │
        │  per-user Hermes 进程（运行时由 broker 动态起）                                   │
        │  /root/.hermes/hermes-agent/  (镜像层,只读)                                       │
        │  /root/.hermes/ (skills/config 缓存, 镜像层 + ConfigMap/Secret 挂载)              │
        │  /var/lib/hermes/sessions  ◄── PVC (RWO)  =  SESSIONS_ROOT                       │
        └──────────────────────────────────────────────────────────────────────────────────┘
                                       │
                ┌──────────────────────┴──────────────────────┐
                ▼                                             ▼
        StatefulSet: postgres:16                      StatefulSet: redis:7
        (阶段一可不接 broker, 仅备)                     (同左)

   独立 Deployment: hermes_api (:8642) / openclaw (:3000) — 各自容器化
```

**为什么 broker + nginx 必须同 pod**：动态端口 `9(1[1-9][0-9]|200)` 路由 + WS Origin 伪造只能在 pod 内做。Ingress 把 `huzhongxiang.cloud` 流量打到 pod，pod 内 nginx 继续 `proxy_pass http://127.0.0.1:$1` 并伪造 Origin——**这套原样保留**。

### 4.2 镜像构建（Dockerfile）

单一镜像，含 broker 运行时 + agent + nginx：

```dockerfile
# ── stage 1: hermes-agent + 补丁 ──
FROM python:3.10-slim AS agent-builder
# 把现网 /root/.hermes/hermes-agent 整体拷入（含 venv），或重新 git clone + 建 venv
COPY hermes-agent /root/.hermes/hermes-agent
COPY patches /tmp/patches
RUN cd /root/.hermes/hermes-agent && \
    for p in /tmp/patches/*.patch; do git apply "$p" || patch -p1 < "$p"; done

# ── stage 2: 运行时 ──
FROM python:3.10-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx git ca-certificates curl && rm -rf /var/lib/apt/lists/*
# Python 依赖（broker）
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt
# agent（含 venv 与补丁）
COPY --from=agent-builder /root/.hermes/hermes-agent /root/.hermes/hermes-agent
# 全局 skills / 共享缓存（只读基线）
COPY hermes-home-base/skills /root/.hermes/skills
COPY hermes-home-base/cache /root/.hermes/cache
# broker 代码
WORKDIR /opt/hermes-platform
COPY . /opt/hermes-platform
# config.yaml / auth.json / .jwt_secret 用挂载覆盖, 不烤进镜像
# nginx 配置（动态端口路由, 由现网 openclaw.conf 改造）
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/default.conf
EXPOSE 80 443
# entrypoint: 起 nginx (后台) → 起 broker (前台, PID 1)
COPY k8s/entrypoint.sh /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
```

> **镜像体积**：agent venv 3.7G，需评估能否瘦身（剥离 `__pycache__`、测试、`.git`、模型缓存）。多阶段 + 固定 base + 节点预拉镜像以降低冷启动。

**`k8s/entrypoint.sh`**：
```bash
#!/usr/bin/env bash
set -e
# 加载 ConfigMap/Secret 注入的环境（k8s 已通过 envFrom 注入, 无需 source .env）
nginx -g 'daemon off;' &                # sidecar
exec python3 /opt/hermes-platform/hermes_broker.py   # PID 1, 收 SIGTERM
```

### 4.3 存储方案（阶段一最大风险点）

| 卷 | 类型 | 挂载点 | 说明 |
|---|---|---|---|
| `hermes-sessions` | **PVC RWO** | `/var/lib/hermes/sessions`（= `SESSIONS_ROOT`） | 全部 per-user 状态；**唯一持久卷**，单 pod 独占 |
| 全局 skills/cache | 镜像只读层 | `/root/.hermes/...` | 不需 PVC |
| `config.yaml` | ConfigMap | `/root/.hermes/config.yaml` | 每 user spawn 时拷贝 |
| `auth.json` | Secret | `/root/.hermes/auth.json` | 凭证池 |
| `.jwt_secret` | Secret（key） | `/opt/hermes-platform/.jwt_secret` | 见 4.6 代码改动 |

- **RWO（ReadWriteOnce）** 够用：阶段一就是单 pod。
- **subPath 注意**：ConfigMap/Secret 挂目录会覆盖，用 `subPath` 挂单文件。
- **不要把 PVC 挂到 `/root/.hermes/`**：那会让全局只读基线被 PVC 覆盖。PVC 只挂 `SESSIONS_ROOT`。

### 4.4 配置与密钥

```
ConfigMap  hermes-config      # SESSIONS_ROOT, BASE_PORT, MAX_PORT, MAX_SESSIONS,
                              # IDLE_TIMEOUT, WARM_POOL_SIZE, API_HOST/PORT,
                              # RATE_LIMIT_*, HERMES_NGINX_DOMAIN, POSTGRES_HOST/PORT/USER/DB(非敏)
Secret     hermes-secret      # POSTGRES_PASSWORD, REDIS_URL, GITHUB_CLIENT_ID/SECRET,
                              # auth.json(整体), 各 model api key
Secret     hermes-jwt         # .jwt_secret (data.jwt=<64B hex>)
ConfigMap  hermes-config-yaml # config.yaml 全文
Secret     hermes-tls         # TLS 证书（或用 cert-manager 自动签发）
```
Pod 用 `envFrom: configMapRef + secretRef` 注入环境；`.jwt_secret` / `auth.json` / `config.yaml` 用 `volume` + `subPath` 挂文件。

### 4.5 网络与 Ingress（动态端口路由如何保留）

- **Ingress**（nginx-ingress 或同类）：终结 TLS，把 `huzhongxiang.cloud` 的所有 location 原样转发到 pod:443（或 :80）。**不在 Ingress 做动态端口路由**——那交给 pod 内 nginx。
- **pod 内 nginx**：把现网 `openclaw.conf` 改造为容器版：
  - upstream 全部指向 `127.0.0.1:<同 pod 端口>`（broker 8080；hermes_api/openclaw 若同 pod 则同 pod 端口，否则改 Service 名）。
  - **保留** `location ~ ^/hermes/ws/9(1[1-9][0-9]|200)/` 及 Origin 伪造、`/hermes/dash/{port}/`、`/hermes/mcp-oauth-cb/{port}/`、`/broker/`、`/auth/`、`/hermes/v1/` 等全部规则。
  - TLS：用 cert-manager 签发，或挂 `hermes-tls` Secret；证书路径改容器内路径。
- **端口暴露**：pod 需暴露 broker(8080) + 全部 9119–9200？不必——**用户只经 Ingress→pod:443→pod nginx→127.0.0.1:{port}**，9119–9200 仅 pod 内回环可见，Service 只暴露 443。

### 4.6 broker 代码改动点（阶段一最小集）

| 文件/位置 | 改动 | 原因 |
|---|---|---|
| `hermes_broker.py:806` `_JWT_SECRET_FILE` | 改为：若挂载的 Secret 文件存在则只读使用，**禁止写回/自生成**（或保留 fallback 但 k8s 下 Secret 必在） | 避免滚动更新让全部 cookie 失效；Secret 不应被 pod 写 |
| `hermes_broker.py:54` `_HERMES_PYTHON` | 保持 `/root/.hermes/hermes-agent/venv/bin/python`（镜像内同路径） | 镜像对齐 |
| broker 日志 | 现 `print`/`logger` 落 `/tmp/broker.log` + 每会话 logs/ | 改 stdout/stderr（k8s 日志聚合）；会话日志仍随 PVC |
| 信号处理 | broker 作为 PID 1 需正确捕获 SIGTERM → 触发 `_flush_and_kill` 全量 graceful | 配合 `terminationGracePeriodSeconds: 45`（flush 3s + 每进程 SIGTERM 10s × 并发） |
| `.env` 加载 | 现 `set -a && source .env` | k8s 下用 `envFrom`，去掉 source；保留对未注入变量的默认值即可 |

> 其余 broker 逻辑（`_spawn`/`acquire`/`release`/软链/端口分配）**全部不改**。

### 4.7 PG / Redis

- 现 `docker-compose.yml` 的 postgres:16 / redis:7 直接翻译为 **StatefulSet + Headless Service + PVC**（数据卷），healthcheck 保留。
- 阶段一 broker 不依赖它们，仅 `api_server` SaaS 模式用；可空挂待用。

### 4.8 生命周期与可观测性

- `StatefulSet`（非 Deployment）：保证 PVC 稳定绑定 + 有序启停。
- `replicas: 1`（阶段一）；`PodDisruptionBudget minAvailable: 0` 允许维护驱逐。
- `livenessProbe`：`GET /broker/health`（broker）；`readinessProbe`：同（broker 起来才接流）。
- `terminationGracePeriodSeconds: 45`：留给 `_flush_and_kill` 持久化 state.db。
- 资源：agent 单进程内存可观，`requests/limits` 按实测给（建议先 limits 高、requests 中等，观察后收敛）。

### 4.9 k8s manifests 清单（阶段一需产出的文件）

```
k8s/
├── namespace.yaml                 # ns: hermes
├── configmap.yaml                 # hermes-config, hermes-config-yaml
├── secret.yaml                    # hermes-secret, hermes-jwt, hermes-tls（或 cert-manager）
├── postgres-statefulset.yaml      # + service + pvc
├── redis-statefulset.yaml         # + service + pvc
├── hermes-statefulset.yaml        # broker+nginx pod, envFrom, volumeMounts, probes
├── hermes-svc.yaml                # ClusterIP :443
├── hermes-pvc.yaml                # RWO sessions 卷
├── ingress.yaml                   # huzhongxiang.cloud → hermes-svc:443
├── hermes-api-deployment.yaml     # :8642（独立）
├── openclaw-deployment.yaml       # :3000（独立）
├── nginx-pod.conf                 # pod 内 nginx（openclaw.conf 容器化版）
└── entrypoint.sh                  # nginx & broker 启动
```

---

## 5. 阶段二架构选项（拆单点，按需启动）

### 5.1 调度模型对比

| 维度 | 每用户一 pod（B） | worker 池调度（D） |
|---|---|---|
| 隔离 | 强（独立 pod/ns） | 中（同 pod 多用户进程） |
| 冷启动 | 慢（pod 起含 3.7G 镜像） | 快（池预热） |
| 资源效率 | 差（每用户常驻 pod） | 好（池复用） |
| broker 改动 | 用 k8s API 建/删 pod；`user→Service` 映射；端口模型废弃 | broker 把用户调度到池节点，仍 `create_subprocess_exec`，但跨节点 |
| 适合 | 高隔离需求、用户数可控 | 高并发、成本敏感 |

### 5.2 broker 内存注册表 → Redis

阶段二多副本前提。复用 `api_server.py` + `ws_pool.py` 已有的多租户 PG+Redis 设计：
- `_procs` 注册表 → Redis hash（`hermes:proc:{user_id}` → {node, port, token, last_active}）
- 端口分配 → Redis 原子 INCR 或 per-node 端口段
- idle 计时 → Redis TTL / 后台扫描
- broker 变成无状态，可水平扩副本

### 5.3 存储演进（sqlite 是阶段二最大约束）

| 方案 | 可行性 | 代价 |
|---|---|---|
| sqlite over RWX（NFS/EFS） | ⚠️ 文件锁不可靠，**不推荐** | 数据损坏风险 |
| state.db 迁 PostgreSQL | ✅ 最稳 | 需改 agent 的持久化层（侵入 hermes-agent） |
| 每 user 一 PVC（RWO） + 每 user 一 pod | ✅ 可行（与 5.1-B 天然匹配） | 用户多时 PVC 数量爆炸，调度受 PVC 亲和约束 |
| uploads/工作文件 → 对象存储（S3/MinIO） | ✅ 配合上述 | 需改上传/读取路径 |

> **建议**：阶段二若走 5.1-B（每用户一 pod），则配「每 user 一 PVC」最省心；若走 5.1-D（池），则必须把 state.db 迁 PG。这个选择决定阶段二工作量量级。

---

## 6. 难点与风险清单（按坑深排序）

| # | 难点 | 影响 | 对策 |
|---|---|---|---|
| 1 | **sqlite + PVC 共享** | 阶段二多 pod 共享用户目录会损坏 | 阶段一 RWO 单 pod 规避；阶段二按 5.3 决策 |
| 2 | **动态端口 + WS Origin 伪造** | 外部 Ingress 做不到 | 阶段一保留 in-pod nginx；阶段二每 pod 单端口+Service |
| 3 | **broker 内存态** | pod 重启丢注册表，用户重连风暴；多副本不一致 | 阶段一接受重连（已支持）；阶段二迁 Redis |
| 4 | **3.7G 镜像冷启动** | 用户首连慢、pod 调度慢 | 镜像瘦身 + 节点预拉 + 池预热 |
| 5 | **JWT secret 自生成** | 滚动更新全量登出 | 改读 Secret（4.6） |
| 6 | **跨层软链绝对路径** | 镜像层→PVC 软链断裂 | 软链目标用容器内稳定绝对路径；skills 软链指向 `/root/.hermes/skills/...` |
| 7 | **SIGTERM → flush 时序** | 强杀丢 state.db | PID 1 捕获信号 + `terminationGracePeriodSeconds: 45` |
| 8 | **egress 到模型/MCP/GitHub** | 集群内网络策略可能阻断 | NetworkPolicy 放行；凭证走 Secret |
| 9 | **openclaw / hermes_api 独立容器化** | 路由依赖它们 | 单独 Deployment + Service，纳入同 Ingress |
| 10 | **TLS 证书** | 现 `/root/.openclaw/.../ssl` | cert-manager + Let's Encrypt，或挂 TLS Secret |

---

## 7. 迁移与回滚步骤

**上线（阶段一）：**
1. 构建镜像（agent + 补丁 + nginx 配置）→ 推 registry。
2. `kubectl apply`：ns / configmap / secret / pg / redis。
3. **数据搬迁**：`rsync -aH /tmp/hermes_sessions/` → 导入 PVC（用临时 pod 挂 PVC 拷入，或 Velero）。校验 84 个 state.db 完整。
4. `kubectl apply`：hermes StatefulSet（先 replicas:0 配置就绪）→ replicas:1。
5. 更新 GitHub OAuth callback URL 为集群域名。
6. Ingress 切流：DNS 把 `huzhongxiang.cloud` 指向集群 LB（或灰度）。
7. 验证：`/broker/health`、OAuth 登录、建会话、技能可见（如 job-posting-assistant）、WS 聊天、上传。

**回滚：** DNS 切回本机 nginx；本机 broker 与 PVC 数据未变（单向拷贝），可立即接管。保持「数据双向同步」前不要双向写。

---

## 8. 工作量估算与里程碑

| 里程碑 | 内容 | 估时 |
|---|---|---|
| M1 镜像化 | Dockerfile + 补丁应用 + nginx-pod.conf + entrypoint + 瘦身 | 2–3 天 |
| M2 manifests | 全套 yaml（含 pg/redis/ingress/tls） | 1–2 天 |
| M3 broker 适配 | JWT/Secret 改动 + 日志 + 信号处理 + `.env` 去依赖 | 1 天 |
| M4 数据搬迁 + 切流 + 验证 | rsync PVC + DNS 切换 + 全功能回归 | 2–3 天 |
| **阶段一小计** | | **~2 周** |
| M5 阶段二决策 | 据并发选 B/D + 存储方案 | 视需求 |
| M6 broker → Redis 注册表 + 调度改造 | | 1–2 周+ |

---

## 9. 附录

### 9.1 需要改的代码位置（阶段一）
- `hermes_broker.py:806` `_JWT_SECRET_FILE` / `_load_or_create_jwt_secret()` → Secret 只读
- broker 启动日志 → stdout
- `entrypoint` 信号转发到 broker（若用 supervisor/nginx 做 PID 1 需 `exec`）
- `.env` 依赖移除（`envFrom` 替代）

### 9.2 nginx 路由全集（须在 pod 内 nginx 保留）
`/hermes/v1/chat/completions`、`/hermes/v1/`、`/hermes/health`、`/broker/`、`/auth/`、`/hermes/ws/9(1[1-9][0-9]|200)/`（WS+Origin伪造）、`/hermes/dash/{port}/`（REST；upload 经 broker）、`/hermes/mcp-oauth-cb/{port}/callback`、`/api/ws`、`/api/(sessions|upload|files|model|skills|...)`、`/v1/chat/completions`、`/v1/messages`、`/chat`。

### 9.3 .env → k8s 映射（见 1.3）

### 9.4 不变的部分
前端 `chat.html`、Hermes WS/REST 协议、OAuth 流程、技能软链机制、`acquire`/`release`/`_spawn`/`_flush_and_kill` 主逻辑、openclaw/hermes_api 业务逻辑。
