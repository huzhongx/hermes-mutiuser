# 架构评审：基于 Hermes 的多租户 AI 工作台 → K8s 迁移下的差距与实现方案

> 编制：2026-06-26
> 基线：`feat/k8s-phase1-lift-and-shift` 分支（k8s 阶段一 lift-and-shift 已完成）
> 对标：Claude Work / Claude Console（Anthropic 的多租户 AI 工作台）
> 范围：站在「即将迁移到 K8s」的节点，逐项盘点**当前欠缺的生产级能力**，并给出**K8s 原生的实现方案**。

---

## 0. 评审基线（当前真实状态）

### 0.1 已具备（k8s 阶段一已交付）
- 单容器镜像（agent + venv + uv python + broker + nginx），分层缓存优化，2.96GB，验证 agent 可起。
- k8s manifests：StatefulSet（单副本）/ Service / Ingress / PG / Redis / ConfigMap / Secret。
- pod 内 nginx 保留动态端口路由 + WS Origin 伪造。
- PVC（RWO）承载 SESSIONS_ROOT。
- broker 代码零改动（配置靠 envFrom/Secret 挂载）。

### 0.2 评审发现的硬事实（代码核查）
- **broker 纯内存态**：`hermes_broker.py` 的 PG/Redis 引用 = **0**；进程注册表、端口分配、idle 计时全在进程内存。
- **api_server 的多租户是 dead code**：`api_server.py` PG/Redis 引用 = 27，但生产 broker 不走它，两套设计并行未接通。
- **agent 用 sqlite**：每个用户 `state.db` 是 SQLite 3 文件，非 PG。
- **无进程沙箱**：broker 用 `create_subprocess_exec` 起 agent，继承 broker 全部权限（root），无 cgroup/seccomp/namespace 隔离。
- **`/broker/files/{user_id}/{filename}` 无 JWT 鉴权**（`hermes_broker.py:1141`），仅靠 user_id 路径参数。

### 0.3 今天已发生的故障（铁证）
- **17:30 OOM**：单机 7.5G 内存撑不住 broker + 十几个用户进程，内核 OOM killer 介入，整条服务链断裂。**单点 + 无资源隔离**的直接后果。

---

## 1. 七大架构支柱的差距与 K8s 实现方案

### 支柱 1：租户与计算隔离

#### 差距
| 维度 | Claude Work | 现状 | 风险 |
|---|---|---|---|
| 计算沙箱 | 每请求/每租户独立（gVisor/Firecracker microVM） | broker subprocess，同 pod 同 namespace 同 root | 用户进程可读他人文件、占满资源拖垮全局 |
| 文件边界 | 租户独立卷 + 权限 | per-user 目录但 root 可读所有 | 数据泄露 |
| 资源限额 | cgroup 严格 CPU/内存 | 无 | OOM（已发生） |

#### K8s 实现方案（分三档）

**短期（阶段一增强）**：在现有单 pod 内加资源边界
- broker StatefulSet 给容器 `resources.limits`（已写，但需实测调优）。
- agent spawn 时传 cgroup：k8s 下用 `sysfs` cgroup v2，broker 在 `_spawn` 后把子进程 PID 写入受限 cgroup（CPU/内存上限）。代码改动：`hermes_broker.py:_spawn` 后加 `_apply_cgroup(pid, mem_limit)`。
- 文件权限：per-user 子目录 `chmod 700`，但同 root 仍可读（治标）。

**中期（阶段二-A：每用户一 pod）**：真正的强隔离
- 用户连接时，broker 调 k8s API 创建专属 Pod（`kind: Pod`，template 含 agent）。
- 每 pod 独立 cgroup / 独立 PVC（RWO per-user）/ 独立网络命名空间。
- Pod 用 `runtimeClassName: kata` 或 `gvisor` 实现微 VM 级隔离。
- 代价：冷启动慢（pod 起含 3GB 镜像），需池化预热。

**长期（阶段二-B：worker 池 + 容器级隔离）**
- 固定 worker 池（Deployment），broker 把用户调度到池节点，每节点用容器级隔离（每用户独立 container，共享 kernel 但 cgroup/namespace 隔离）。
- 折中方案：隔离弱于 pod，但冷启动快、资源效率高。

#### 推荐路径
阶段一先加 cgroup 资源限额（防 OOM），阶段二按并发量决定走 A（强隔离）还是 B（高密度）。

---

### 支柱 2：数据模型与多租户状态

#### 差距
- **broker 内存态 vs api_server PG 设计未接通**：生产用前者（无持久化、无水平扩展），后者是设计好的多租户 PG schema 却没启用。
- **agent sqlite 难共享**：多 pod 共享用户目录时 sqlite 文件锁不可靠（k8s 迁移方案 §5.3 已识别）。
- **无 token/成本记账**：`messages.tokens` 字段有但无人填。
- **无审计/保留策略**。

#### K8s 实现方案

**状态外部化（P0）**
- broker 进程注册表从内存迁 **Redis**（api_server 已有 `redis_pool`，broker 接入即可）：
  - `hermes:proc:{user_id}` → `{node, port, token, last_active}`（Redis hash）
  - 端口分配 → Redis 原子 INCR 或 per-node 端口段
  - idle 计时 → Redis TTL + 后台扫描
- broker 变无状态 → 可水平扩副本。

**agent state.db 演进（P2，按阶段二调度模型决定）**
- 每用户一 pod 模式：每用户一 PVC（RWO），sqlite 不共享，零改动。
- worker 池模式：sqlite 迁 PG（需改 hermes-agent 持久化层，侵入大）。

**数据模型补全（P1）**
在 `scripts/init_db.sql` 加表：
```sql
CREATE TABLE usage (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES tenants(id),
    session_id UUID,
    model VARCHAR(64),
    prompt_tokens INTEGER, completion_tokens INTEGER,
    cost_usd NUMERIC(10,6),
    recorded_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE TABLE audit_log (
    id BIGSERIAL PRIMARY KEY,
    tenant_id UUID, actor_id VARCHAR(128),
    action VARCHAR(64), resource_type VARCHAR(32), resource_id VARCHAR(128),
    detail JSONB, ip_address INET, created_at TIMESTAMPTZ DEFAULT NOW()
);
-- 数据保留：加 retention 配置 + 定期清理 job
```

---

### 支柱 3：水平扩展与调度

#### 差距
- broker 单点（已识别，阶段二目标）。
- 无跨节点 WS 会话亲和。

#### K8s 实现方案

**broker 无状态化（P0）**
- 注册表迁 Redis（见支柱 2）后，broker 可 `replicas: N`。
- 前置 Service + Ingress 负载均衡；WS 用 `nginx.ingress.kubernetes.io/affinity: cookie` 做 sticky。

**调度器（P2）**
- 每用户一 pod：broker 内加 `K8sScheduler`，用 `kubernetes` python client 调 `create_namespaced_pod`。
- worker 池：broker 维护 `{node: load}`，acquire 时选最闲节点，通过该节点上的本地 agent spawner 起进程（节点间用 gRPC/HTTP 通信）。

**会话路由（P2）**
- 多副本下，用户 WS 必须连到托管其 agent 的那个 broker/pod。
- 方案：Redis 记 `{user_id → pod_name}`，Ingress 层或 broker 间重定向；或用 client-side service discovery。

---

### 支柱 4：可观测性（几乎全缺）

#### 差距
- 日志：`logging.basicConfig` → 非结构化，无 trace_id/tenant_id。
- 无 metrics、无 tracing、无错误聚合。
- 多跳链路（Ingress→broker→per-user agent→openclaw→模型上游）出问题无法定位。

#### K8s 实现方案（生态成熟，成本低）

**结构化日志（P0）**
- 改 `logging.basicConfig` → JSON formatter（`python-json-logger`），每条带 `tenant_id/session_id/trace_id`。
- k8s 下 stdout 自动被 Loki/ELK 采集。

**Metrics（P0）**
- broker 暴露 `/metrics`（Prometheus 格式），用 `prometheus_client`：
  - `hermes_active_sessions`（gauge，per-tenant label）
  - `hermes_spawn_total` / `hermes_spawn_failures_total`（counter）
  - `hermes_api_latency_seconds`（histogram）
  - `hermes_model_upstream_errors`（counter，per-model）
- k8s 加 `ServiceMonitor`（Prometheus Operator）自动抓取；Grafana 看板。

**分布式追踪（P1）**
- OpenTelemetry：broker + agent 注入 trace context，贯穿 nginx→broker→agent→模型。
- 用 OpenTelemetry Operator 自动 sidecar 注入，导出到 Jaeger/Tempo。

**错误聚合 + 告警（P1）**
- Sentry SDK 接入 broker/agent。
- Prometheus Alertmanager：P90 延迟、5xx 率、spawn 失败率、OOM 事件告警。

---

### 支柱 5：安全与密钥管理

#### 差距
- `/broker/files/{user_id}/{filename}` **无 JWT 鉴权**（可越权读他人文件）。
- 凭证明文存盘（`auth.json`、config 凭证池）。
- 无 RBAC（租户内无角色）。
- JWT 单密钥，轮换难。
- `.env` 明文密钥（dev 默认值已进 git）。

#### K8s 实现方案

**鉴权补齐（P0，代码改）**
- `/broker/files/*` 改用 `Depends(verify_session_user)`（broker 已有 `_require_session_user`，复用）。
- 审查所有 `user_id` 路径参数的 endpoint，统一改 JWT。

**密钥管理（P0）**
- **External Secrets Operator** + Vault / 云 KMS：k8s Secret 不再手动 create，由 ESO 从 Vault 同步。
- 凭证池加密存储：`auth.json` 用 Fernet/AES 加密，密钥由 KMS 托管，运行时解密。
- `.env` 彻底移出 git（已在 .gitignore，但历史 commit 有 dev 默认值，需 `git filter-repo` 清理 + 轮换所有密钥）。

**RBAC（P2）**
- 租户内加 `role`（admin/member/viewer），`tenants` 表加 `members` 关联表 + 权限矩阵。
- broker endpoint 按 role 鉴权。

**密钥轮换（P1）**
- JWT 改双密钥（primary/secondary）平滑轮换：新请求用新密钥签，旧密钥仍可验，定期淘汰旧密钥。
- 凭证池支持热重载（broker 已有 `/broker/reload-mcp`，扩展到凭证）。

---

### 支柱 6：模型层与成本控制

#### 差距
- 无 per-tenant token 配额（一个用户可烧光所有额度）。
- 无模型故障转移（primary 挂了不会自动切 fallback）。
- 无成本看板/预算告警。

#### K8s 实现方案

**per-tenant 配额（P1）**
- 在 broker 调用模型前，查 Redis/PG 的 tenant 配额：
  ```python
  # 伪代码，加在 broker 代理模型调用处
  used = await redis.incrby(f"usage:{tenant_id}:tokens:{today}", tokens)
  if used > tenant.token_quota: raise HTTPException(429, "quota exceeded")
  ```
- 配额配置在 `tenants` 表加 `token_quota_daily` 字段。

**模型故障转移（P1）**
- agent 已有 `fallback_providers` 概念（config.yaml），broker 层补一个 provider 健康检查：
  - Prometheus 监控每 provider 错误率，超阈值标记不健康。
  - broker 路由时跳过不健康 provider，自动切 fallback。
- 92s 空完成（之前排查的）这类问题，靠熔断 + 自动重试 fallback 缓解。

**成本看板（P2）**
- `usage` 表（支柱 2）按 tenant/model 聚合 → Grafana 看板。
- 预算告警：用量达 80%/100% 触发 Alertmanager → 飞书/邮件。

---

### 支柱 7：交付与运维（CI/CD + IaC + 灾备）

#### 差距
- 无 CI/CD（手动 `service.sh`）。
- 无 IaC（manifests 裸 yaml，无环境管理）。
- 无备份/灾备（sqlite 在 PVC，无快照）。
- 无镜像扫描/金丝雀发布。

#### K8s 实现方案

**CI/CD（P1）**
- GitLab CI（已有 GitLab remote）：`.gitlab-ci.yml`
  - test → build（基础镜像 + 应用镜像）→ trivy 扫描 → push registry → 触发 deploy
- 镜像分两类：`hermes-base`（agent，少变）+ `hermes-app`（broker 代码，常变），见 `docs/k8s-base-image-plan.md`。

**IaC（P1）**
- 现有 `k8s/*.yaml` 改造成 **Helm chart** 或 **Kustomize overlays**（dev/staging/prod）。
- 密钥统一走 External Secrets，不进 chart。

**灾备（P2）**
- PG：CloudNativePG / pg-operator，开启 PITR（point-in-time recovery）+ 定期全量备份到对象存储。
- PVC（sessions）：VolumeSnapshot 定期快照；或把 sessions 迁对象存储（S3/MinIO）。
- 跨可用区：阶段二多副本分散到不同 node/az。

**发布策略（P2）**
- Argo Rollouts / Flagger：金丝雀（先 10% 流量新版本，指标正常再全量）。
- broker 无状态化后才能平滑发布。

---

## 2. 优先级总表（K8s 迁移视角）

| 优先级 | 支柱 | 事项 | 阻塞 k8s 迁移？ | 代码侵入 |
|---|---|---|---|---|
| **P0** | 1 | 容器资源 limits（防 OOM） | 是 | 小（manifest） |
| **P0** | 5 | `/broker/files` 鉴权补齐 | 否（安全债） | 小 |
| **P0** | 4 | 结构化日志 + Prometheus metrics | 否 | 中 |
| **P0** | 2 | broker 注册表迁 Redis | 是（水平扩展前提） | 中 |
| **P1** | 6 | per-tenant token 配额 | 否（成本债） | 中 |
| **P1** | 1 | agent spawn 加 cgroup 资源限制 | 否 | 中 |
| **P1** | 5 | External Secrets + 凭证加密 | 否 | 中 |
| **P1** | 7 | CI/CD + Helm | 否 | 小 |
| **P2** | 3 | 每用户 pod / worker 池调度 | 是（阶段二核心） | 大 |
| **P2** | 4 | OpenTelemetry 全链路追踪 | 否 | 中 |
| **P2** | 2 | usage/audit 表 + 数据保留 | 否 | 中 |
| **P3** | 5 | RBAC | 否 | 中 |
| **P3** | 7 | 灾备 + 金丝雀发布 | 否 | 小 |

---

## 3. K8s 迁移路线图（整合差距项）

```
阶段一（已完成）：lift-and-shift 单 pod，验证可跑
   └─ 补 P0：容器 limits、日志、metrics、/broker/files 鉴权

阶段一.五（加固）：broker 无状态化
   └─ P0：注册表迁 Redis → broker 可扩副本
   └─ P1：token 配额、cgroup、External Secrets、CI/CD

阶段二（水平扩展）：拆单点
   └─ P2：每用户 pod 或 worker 池调度
   └─ P2：usage/audit、OTel 追踪、数据保留
   └─ P2：灾备 + 金丝雀

阶段三（生产成熟）：
   └─ P3：RBAC、强隔离（gVisor/Kata）、跨可用区
```

---

## 4. 一句话总结

当前平台具备 Claude Work 的**用户可见形态**（多用户、多会话、流式、技能、文件面板），k8s 阶段一解决了**计算交付层**（能跑在集群）。但距离生产级多租户 SaaS，还缺**七大支柱的生产内核**：计算隔离、状态外移、水平扩展、可观测、安全、成本控制、自动化运维。

好消息：**这些差距在 K8s 生态里都有成熟方案**（cgroup/Redis/Prometheus/OTel/External Secrets/Helm/Argo），不需要自研基础设施，关键是按优先级把代码侧改造逐项落地。其中 **broker 无状态化（注册表迁 Redis）是枢纽** —— 它解锁水平扩展，是阶段二一切的前提。

---

## 附录：与现有文档的关系

- `docs/k8s-migration-plan.md`：总迁移方案（两阶段、存储/网络/镜像）。本文是它的**能力差距补充**。
- `docs/k8s-base-image-plan.md`：镜像分层（base + app）。本文支柱 7 的 CI/CD 基于此。
- `k8s/README.md`：构建部署操作指南。
- 本文聚焦：**迁移到 K8s 后，还缺什么、怎么补**。
