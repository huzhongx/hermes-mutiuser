# Hermes Platform → Kubernetes 阶段一（lift-and-shift）

> 配套方案见 `docs/k8s-migration-plan.md`。
> **核心约束：本套件不修改 `hermes_broker.py`，不影响本机正在运行的单机版。**
> 所有容器化行为通过新增的隔离文件实现（`Dockerfile` + `k8s/`）。

## 设计要点（为什么零改 broker）

| k8s 需求 | 如何实现（不改源码） |
|---|---|
| 配置注入 | `envFrom`（ConfigMap + Secret）替代 `source .env` |
| JWT 密钥 | Secret 挂到 `/opt/hermes-platform/.jwt_secret`；broker 现有代码读到即用、不写回（OSError fallback 已对只读挂载安全） |
| 日志采集 | broker 默认输出 stderr；容器不重定向 → k8s 自动采集 |
| 优雅退出 | SIGTERM → uvicorn `--timeout-graceful-shutdown=50` → FastAPI lifespan → `broker.stop()` 已 flush+kill 全部子进程 |
| config.yaml / auth.json | ConfigMap / Secret 用 `subPath` 挂到 `/root/.hermes/`（不覆盖整个镜像基线目录） |
| 会话状态 | PVC（RWO）挂到 `SESSIONS_ROOT=/var/lib/hermes/sessions` |
| 动态端口 + Origin 伪造 | pod 内 nginx sidecar 原样保留生产路由（`k8s/nginx-pod.conf`） |

## 文件清单

```
Dockerfile                       多阶段镜像（agent + hermes-home 基线 + nginx + broker）
.dockerignore                    排除 .env/.jwt_secret/sessions/缓存
k8s/
├── requirements-image.txt       镜像用 Python 依赖（base + pyjwt + requests）
├── prep-build-context.sh        把现网 agent + 全局基线暂存到 k8s/build/
├── nginx-pod.conf               pod 内 nginx（生产 openclaw.conf 容器化版）
├── 00-namespace.yaml            ns: hermes
├── 10-config.yaml               ConfigMaps（hermes-config / hermes-config-yaml / hermes-nginx）
├── 11-secret.yaml               Secret 模板（占位，部署前替换）
├── 20-postgres.yaml             PG StatefulSet（阶段一可不用）
├── 21-redis.yaml                Redis StatefulSet（阶段一可不用）
├── 30-hermes-svc.yaml           headless Service（StatefulSet + Ingress 后端）
├── 31-hermes-statefulset.yaml   ★ nginx sidecar + broker 容器（核心）
├── 32-ingress.yaml              TLS + 路由到 pod:80
├── 40-extras.yaml               hermes-api / openclaw 占位（镜像待独立构建）
└── kustomization.yaml           apply 顺序
```

## 构建镜像

```bash
cd /opt/hermes-platform

# 1) 准备构建上下文：暂存现网 agent + 全局基线（已打补丁版本，原样 lift）
bash k8s/prep-build-context.sh
#    → k8s/build/hermes-agent/（约 1.5G，剔除 node_modules/tests/website）
#    → k8s/build/hermes-home-base/（skills/cache/pairing/共享 json）

# 2) 构建镜像
docker build -t hermes-platform:phase1 .

# 3) 推到集群可访问的 registry（按实际改 tag）
docker tag hermes-platform:phase1 <registry>/hermes-platform:phase1
docker push <registry>/hermes-platform:phase1
# 并改 31-hermes-statefulset.yaml 的 image（或 kustomization images:）
```

## 配置（部署前必填）

```bash
# 1) 填 ConfigMap 真实值：编辑 10-config.yaml（HERMES_NGINX_DOMAIN、RATE_LIMIT_* 等）
#    并把本机 /root/.hermes/config.yaml 全文粘到 hermes-config-yaml 的 config.yaml。

# 2) 用真实密钥建 Secret（勿提交占位文件）：
kubectl -n hermes create secret generic hermes-secret \
  --from-literal=GITHUB_CLIENT_ID=<id> \
  --from-literal=GITHUB_CLIENT_SECRET=<secret> \
  --from-literal=POSTGRES_PASSWORD=<pw> \
  --from-literal=REDIS_URL=redis://:<pw>@redis:6379/0 \
  --from-file=auth.json=/root/.hermes/auth.json

# 3) JWT 密钥：拷贝本机现值，保证现有 cookie 不失效
JWT=$(cat /opt/hermes-platform/.jwt_secret)
kubectl -n hermes create secret generic hermes-jwt --from-literal=jwt="$JWT"

# 4) TLS：用 cert-manager 自动签发，或手工
kubectl -n hermes create secret tls hermes-tls \
  --cert=/root/.openclaw/workspace/openclaw-router/ssl/huzhongxiang.cloud_bundle.pem \
  --key=/root/.openclaw/workspace/openclaw-router/ssl/huzhongxiang.cloud.key
```
> 若已用上面的 `create secret` 注入，则 `kubectl apply -k` 前先把 `kustomization.yaml` 里的 `11-secret.yaml` 去掉，避免占位覆盖真实 Secret。

## 数据迁移（会话状态）

```bash
# PVC 由 volumeClaimTemplates 自动创建。用一个临时 pod 挂上它，把本机数据 rsync 进去。
# 示例（假设 StorageClass 支持，PVC 名 sessions-hermes-0）：
kubectl -n hermes run rsync-helper --image=busybox -it --rm \
  --overrides='{"spec":{"containers":[{"name":"rsync-helper","image":"busybox","command":["sleep","3600"],"volumeMounts":[{"name":"v","mountPath":"/data"}]}],"volumes":[{"name":"v","persistentVolumeClaim":{"claimName":"sessions-hermes-0"}}]}}' -- sh

# 另一终端：本机 → pod
SRC=/tmp/hermes_sessions/
kubectl -n hermes cp "$SRC" rsync-helper:/data/sessions
# 校验：state.db 数量、体积
```
> sqlite 文件锁：阶段一 RWO 单 pod 安全。阶段二多 pod 共享需另选方案（见 plan §5.3）。

## 部署

```bash
kubectl apply -k k8s/
kubectl -n hermes rollout status statefulset/hermes

# 验证 broker
kubectl -n hermes port-forward svc/hermes 8080:80
curl -s http://127.0.0.1:8080/broker/health   # 期望 {"status":"ok",...}
```

## 切流 & 验证

1. **OAuth callback**：GitHub OAuth app 的 callback URL 改成集群域名（`https://<domain>/auth/callback`）。
2. **DNS**：把 `<HERMES_NGINX_DOMAIN>` 解析切到集群 Ingress LB（可灰度）。
3. **功能回归**：
   - OAuth 登录 → 建会话 → `/broker/sessions` 返回 ws_url
   - WS 聊天（`/hermes/ws/{port}/api/ws`，Origin 伪造链路）
   - 技能可见（如 `job-posting-assistant`）
   - 文件上传 → `/broker/upload` → WS `image.attach` → `prompt.submit`
   - REST 历史取回（`/hermes/dash/{port}/api/sessions`）

## 回滚

DNS 切回本机 nginx；本机 broker 与数据未变（迁移是单向拷贝），立即接管。
**切流前不要让两端同时写** PVC（sqlite 单写者）。

## 已知边界（阶段一）

- **单点**：replicas=1，broker 内存注册表，pod 重启 → 用户重连重分配（已支持）。
- **openclaw / hermes-api**：需独立容器化（`40-extras.yaml` 占位）；未就绪前 `/v1/* /api/* /hermes/v1/*` 路由 503，broker/per-user 链路不受影响。
- **无水平扩展**：阶段二才拆单点（注册表迁 Redis + 调度改造，见 plan §5）。
