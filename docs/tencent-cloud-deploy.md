# 腾讯云主机迁移部署文档

> 目标：将现网单台 Ubuntu 主机（broker + per-user Hermes agent）迁移到一台腾讯云 CVM
> 基础：现网已有完整的 Docker 镜像（`hermes-platform:v1`，3.28GB）和 build 链路（`feat/k8s-phase1-lift-and-shift` 分支）
> 适用读者：执行迁移的运维 / 平台工程师

---

## 0. 与阿里云方案的差异（为什么腾讯云更省事）

| 维度 | 阿里云 ECS | 腾讯云 CVM | 差异影响 |
|---|---|---|---|
| **基础镜像** | Docker Hub `python:3.11-slim`（公共） | **现网私有 `qx-images.tencentcloudcr.com/qunxing/python:3.11-slim` 可直接用** | 腾讯云**不用改 Dockerfile**，更省事 |
| 镜像仓库 | ACR（个人版免费） | TCR（个人版免费） | 两者都 OK |
| DNS | 阿里云 DNS | DNSPod（腾讯云旗下，免费） | 体验略优 |
| SSL | 阿里云免费 DV | 腾讯云免费 DV (TrustAsia) | 两者都 OK |
| 对象存储 | OSS（备份） | COS（备份） | 两者都 OK |
| **核心结论** | Dockerfile 要改基础镜像 | **Dockerfile 原样可用** | 腾讯云**少一步改动** |

---

## 1. CVM 选型（避免 OOM 重演）

### 1.1 为什么不能选小的

现网 7.5G 内存 + broker + 十几个 hermes agent + node venv = 历史上 17:30 已发生 OOM（kernel killer 介入）。迁移是改造的最佳时机，**别只是搬石头**。

### 1.2 推荐配置

| 配置 | 内存 | 适用 | 备注 |
|---|---|---|---|
| S5.SMALL8 | 8G | 个人/小团队测试（< 5 活跃用户）| ⚠️ 仍偏紧 |
| S5.MEDIUM8 | 8G | 标准起步 | 仅适合并发极低 |
| **S5.LARGE16 (4C16G)** ⭐ | **16G** | **推荐生产**（≥ 现网负载 + 余量）| broker 限 12G，系统预留 4G |
| S5.2XLARGE32 (8C32G) | 32G | 重度/有 growth plan | 充裕 |

**建议：S5.LARGE16（标准型 S5，4 核 16GB）**，约 ¥300/月（北京/上海），系统盘 100G 高效云盘（数据 1.2G + 镜像 3.3G + 日志空间）。

### 1.3 镜像选择

- **系统镜像**：Ubuntu Server 22.04 LTS 64位（与现网一致，迁移兼容性最好；24.04 也可）
- **带宽**：5 Mbps 起步（用户不多够用；流量大按需升级）

---

## 2. CVM 安全组（第一道防火墙）

腾讯云控制台 → CVM → 安全组 → 入站规则：

| 协议端口 | 来源 | 说明 |
|---|---|---|
| TCP:22 | 你的固定 IP（必需） | SSH |
| TCP:80 | 0.0.0.0/0 | HTTP（certbot 验证 + 跳转 HTTPS） |
| TCP:443 | 0.0.0.0/0 | HTTPS（生产） |

**出站**：全放开（agent 要调模型/MCP/GitHub OAuth 等）。

---

## 3. 域名 + SSL（迁移前置）

### 3.1 域名

- 推荐：腾讯云 DNSPod（免费、自动集成 CVM）
- 也可：阿里云 DNS / Cloudflare 等任意 DNS 服务商

DNS 配置：`hermes.yourdomain.com` → CVM 公网 IP（A 记录）。

### 3.2 SSL 证书

**腾讯云免费证书**（推荐）：
1. https://console.cloud.tencent.com/ssl → 申请免费证书 → 单域名
2. 域名验证方式选 **DNS 验证**（DNSPod 自动加解析记录，秒过）
3. 下载 Nginx 格式（含 `fullchain.pem` + `privkey.pem`）

**替代：Let's Encrypt + certbot**（免费、自动续期）：
```bash
certbot certonly --standalone -d hermes.yourdomain.com
# 证书路径: /etc/letsencrypt/live/hermes.yourdomain.com/{fullchain.pem,privkey.pem}
```

**关键**：OAuth callback 必须 HTTPS。证书准备好再迁移。

---

## 4. CVM 初始化

### 4.1 SSH 登录 + 系统更新

```bash
ssh root@<CVM_IP>
apt update && apt upgrade -y
# 必备工具
apt install -y nginx certbot rsync curl jq htop
# 时区（避免日志时间错乱）
timedatectl set-timezone Asia/Shanghai
```

### 4.2 Docker + docker-compose

```bash
# 腾讯云 CVM 默认未装 docker
curl -fsSL https://get.docker.com | bash
# 验证
docker version
# 安装 docker compose plugin（v2）
apt install -y docker-compose-plugin
docker compose version
```

### 4.3 （可选）腾讯云容器镜像服务 TCR 配置

**强烈推荐**——把构建好的 `hermes-platform:v1` 推到 TCR，CVM 拉取走内网（免费、极快），比 docker hub 快 10 倍。

```bash
# 1. 控制台 https://console.cloud.tencent.com/tcr → 创建个人版实例
# 2. 在 TCR 控制台 → 访问凭证 → 生成临时登录凭证
# 3. CVM 上登录
docker login ccr.ccs.tencentyun.com --username=<临时用户名>
# 4. 镜像改名（符合 TCR 命名规范）
docker tag hermes-platform:v1 ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1
docker push ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1
```

后续 `docker-compose.yml` 里就用 `image: ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1`。

---

## 5. 数据迁移（**最关键的步骤**）

### 5.1 现网打包

**在现网机器上执行**：

```bash
# 打包 broker 运行所需的全局配置 + 用户会话
# 注意: .git/claude/portal/test/* 都不要
tar -czf /tmp/hermes-migrate.tar.gz \
    /tmp/hermes_sessions \
    /root/.hermes/hermes-agent \
    /root/.hermes/skills \
    /root/.hermes/auth.json \
    /root/.hermes/config.yaml \
    /root/.hermes/.env \
    /root/.hermes/cache /root/.hermes/pairing \
    /root/.hermes/models_dev_cache.json \
    /root/.hermes/ollama_cloud_models_cache.json \
    /root/.hermes/broker_server_* \
    /root/.hermes/fastapi_server_* \
    --exclude='*.corrupt.*' --exclude='*.bak*' \
    --exclude='audio_cache' --exclude='image_cache'

ls -lh /tmp/hermes-migrate.tar.gz
```

⚠️ **tar 包含明文 `auth.json/.env`，含生产 API key**：
- 只通过受信任 SSH 传输
- 传输完立刻删除本地 tar 文件
- **迁移完成后立刻轮换所有密钥**（§11）

### 5.2 传输到 CVM

```bash
scp /tmp/hermes-migrate.tar.gz root@<CVM_IP>:/root/
# 本地删除（保险）
shred -u /tmp/hermes-migrate.tar.gz
```

### 5.3 CVM 解压

```bash
ssh root@<CVM_IP>
cd /root && tar -xzf hermes-migrate.tar.gz -C /
# 验证数据完整性
ls /tmp/hermes_sessions/ | wc -l              # 应 ≈ 80+ 用户会话
ls /tmp/hermes_sessions/huzhongx/hermes_home/state.db  # 文件应存在
# 关键: 清理 tar 包
shred -u /root/hermes-migrate.tar.gz
```

---

## 6. Docker 镜像构建（在 CVM 上）

**关键优势**：现网 Dockerfile 已经用 `qx-images.tencentcloudcr.com/qunxing/python:3.11-slim`，**腾讯云 CVM 可直接拉**，**不需要替换基础镜像**。

### 6.1 克隆代码 + 切到正确分支

```bash
cd /opt
git clone <repo-url> hermes-platform
cd hermes-platform
git checkout feat/k8s-phase1-lift-and-shift
```

### 6.2 构建上下文（service.sh 会做）

```bash
# HERMES_HOME 默认从 /root/.hermes 取（步骤 5.3 已恢复）
bash k8s/prep-build-context.sh
# 输出:
#   ==> 同步现网 agent（含 venv + 已应用补丁）到构建上下文 ./hermes-agent
#   ==> 同步 uv python（venv 解释器，104M）到构建上下文 ./uv-python
#   ==> 同步 /root/.hermes 全局只读资源到构建上下文 ./hermes
```

### 6.3 构建

```bash
# 直接 build（基础镜像可拉取）
docker build -f docker/Dockerfile -t hermes-platform:v1 . 2>&1 | tail -20
# 输出末行应是:
#   Successfully tagged hermes-platform:v1

# 验证大小
docker images hermes-platform:v1 --format '{{.Size}}'
# 期望: 约 3.28GB
```

### 6.4 验证镜像（构建后立刻测）

```bash
# 短暂容器测试 agent venv 可起
docker run --rm --entrypoint /root/.hermes/hermes-agent/venv/bin/python hermes-platform:v1 \
    -c 'import hermes_cli.main; print("✓ agent 加载 OK")'
# 期望: ✓ agent 加载 OK
```

清理 build context（可选，节省磁盘）：
```bash
rm -rf /opt/hermes-platform/{hermes-agent,uv-python,hermes}
```

### 6.5 推 TCR（可选，但推荐）

```bash
docker tag hermes-platform:v1 ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1
docker push ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1
```

---

## 7. docker-compose 部署（生产形态）

### 7.1 写 `docker-compose.yml`

```yaml
version: "3.9"

services:
  hermes:
    # 二选一：
    # A. 本地构建的镜像
    # image: hermes-platform:v1
    # B. 从 TCR 拉（推荐）
    image: ccr.ccs.tencentyun.com/<命名空间>/hermes-platform:v1
    container_name: hermes
    restart: always
    ports:
      - "8088:8080"   # broker（如果想外部访问；否则只暴露 80/443 即可）
    environment:
      SESSIONS_ROOT: /var/lib/hermes/sessions
      HERMES_NGINX_DOMAIN: hermes.yourdomain.com
      HERMES_PUBLIC_HOST: "127.0.0.1"
    volumes:
      - sessions-data:/var/lib/hermes/sessions
      - /root/.hermes/auth.json:/root/.hermes/auth.json:ro      # 凭证池（只读）
      - /root/.hermes/.env:/root/.hermes/.env:ro                # 凭证（只读）
      - /root/.hermes/config.yaml:/root/.hermes/config.yaml:ro  # 配置（只读）
      # secrets 挂载：这是"测试场景"做法。生产推荐用 k8s Secret/External Secrets，
      # 这里单机方案直接 mount 文件是简单稳妥的方式。
    healthcheck:
      test: ["CMD-SHELL", "curl -fs http://localhost/broker/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s   # broker 启动需要时间
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
    deploy:
      resources:
        limits:
          memory: 12G    # broker + 所有 per-user 进程上限（防 OOM 直接落地）
        reservations:
          memory: 1G
    healthcheck:
      # ... 同上

volumes:
  sessions-data:
    name: hermes-sessions-data
```

### 7.2 启动

```bash
cd /opt/hermes-platform
docker compose up -d
docker compose ps
docker compose logs -f hermes
```

**预期日志**：
```
[entrypoint] starting nginx sidecar...
[entrypoint] nginx ready (pid XX)
[entrypoint] starting broker (uvicorn)...
INFO: Started server process
INFO: Application startup complete.
INFO: Uvicorn running on http://0.0.0.0:8080
hermes_broker: ProcessBroker started, warm_pool=5
```

### 7.3 容器内 broker + nginx 都该监听

```bash
docker exec hermes ss -tln | grep -E ':80|:8080'
# 预期:
#   LISTEN  0  511  0.0.0.0:80     0.0.0.0:*
#   LISTEN  0  2048 0.0.0.0:8080  0.0.0.0:*
```

---

## 8. 宿主机 nginx（SSL termination + 转发）

容器内 nginx 监听 80，做动态端口路由 + WS Origin 伪造。宿主机 nginx 再包一层做 **HTTPS 终结** + 转发到容器 80。

### 8.1 写 `/etc/nginx/sites-available/hermes`

```nginx
# HTTP → HTTPS 跳转
server {
    listen 80;
    server_name hermes.yourdomain.com;
    return 301 https://$host$request_uri;
}

# HTTPS
server {
    listen 443 ssl http2;
    server_name hermes.yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/hermes.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hermes.yourdomain.com/privkey.pem;

    client_max_body_size 50m;
    client_body_timeout  300s;

    # 全局流量 → 容器内 nginx (它负责动态端口 + WS Origin 伪造)
    location / {
        proxy_pass http://127.0.0.1:80;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        # WebSocket 升级
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        # 长超时
        proxy_read_timeout 600s;
        proxy_send_timeout 300s;
        proxy_buffering off;
    }
}
```

### 8.2 启用 + 验证

```bash
ln -sf /etc/nginx/sites-available/hermes /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default  # 避免冲突
nginx -t                              # 验证配置
systemctl reload nginx
curl -I https://hermes.yourdomain.com/broker/health
# 预期: HTTP/2 200
```

---

## 9. SSL 申请（迁移前置之一）

如果你还没申请证书：

```bash
# 安装 certbot
apt install -y certbot

# 先把 DNS 解析切到 CVM（重要：certbot 要能访问你的域名）
# 阿里云/DNSPod 控制台 → 解析到 CVM 公网 IP

# 申请证书（standalone 模式：会临时占用 80 端口，要先停 nginx 或用 webroot）
certbot certonly --standalone -d hermes.yourdomain.com \
    --email admin@yourdomain.com --agree-tos --no-eff-email

# 证书路径
ls /etc/letsencrypt/live/hermes.yourdomain.com/
# fullchain.pem  privkey.pem
```

**certbot 自动续期**（已配 systemd timer）：
```bash
certbot renew --dry-run   # 测试
```

---

## 10. DNS 切换（核心操作，谨慎）

### 10.1 切流前的双轨运行期（推荐）

**不要**立刻切 DNS！建议流程：
1. CVM 上 broker 完整跑通、容器稳定
2. 用本地 hosts 或子域名（test.hermes.yourdomain.com）CVM 访问验证
3. 跑完整功能测试（登录、发消息、上传文件、WS 连接）
4. 观察 24-48 小时
5. 切换主域 DNS

### 10.2 切换 DNS

DNSPod 控制台 → hermes.yourdomain.com → 记录值改为 CVM 公网 IP。

TTL 建议设为 300 秒（5 分钟），切换生效快。

### 10.3 验证

```bash
# 在 CVM 上确认
curl https://hermes.yourdomain.com/broker/health
# 预期: {"status":"ok",...}

# 在外网
nslookup hermes.yourdomain.com    # 确认解析到新 IP
```

---

## 11. **关键：轮换所有密钥**（迁移后立刻做）

⚠️ tar 包传输含明文凭证。**轮换是必须的**，别跳过：

```bash
# 1. GitHub OAuth App
#    https://github.com/settings/developers
#    → 重新生成 client secret
#    → 更新 .env (然后重启 broker: docker compose restart hermes)

# 2. openclaw router API key
#    现在镜像里 auth.json 拷贝的旧 key
#    → 登录 openclaw 控制台 → 重置 → 替换 auth.json
docker compose restart hermes

# 3. 其他 model API key
#    检查 .env 里的 OPENAI_API_KEY 等
#    在对应 provider 控制台重置
```

**轮换完，删除 CVM 上残留的 tar 备份**（如果有）：
```bash
shred -u /root/hermes-migrate.tar.gz  # 如果还在
```

---

## 12. 数据备份策略（推荐）

每天将 sessions-data 命名卷同步到腾讯云 COS（对象存储）：

```bash
# 1. 安装 ossutil
curl -O https://gosspublic.alicdn.com/ossutil/1.7.18/ossutil64
chmod +x ossutil64 && ./ossutil64 config

# 2. 备份脚本（crontab）
cat > /usr/local/bin/hermes-backup.sh <<'EOF'
#!/bin/bash
set -e
DATE=$(date +%Y%m%d)
docker run --rm \
  -v hermes-sessions-data:/data:ro \
  -v /tmp:/backup \
  alpine tar czf /backup/hermes-sessions-${DATE}.tar.gz -C /data .
# 上传到 COS（用 ossutil 或 coscli）
coscli cp /tmp/hermes-sessions-${DATE}.tar.gz cos://your-bucket/hermes/backups/
# 清理 30 天前的本地备份
find /tmp -name 'hermes-sessions-*.tar.gz' -mtime +30 -delete
EOF
chmod +x /usr/local/bin/hermes-backup.sh

# 3. 每日凌晨 3 点执行
echo "0 3 * * * root /usr/local/bin/hermes-backup.sh" > /etc/cron.d/hermes-backup
```

---

## 13. 监控与告警（最小集）

### 13.1 关键监控指标

| 指标 | 命令/方式 | 告警阈值 |
|---|---|---|
| broker 健康 | `curl /broker/health` | 连续 3 次失败 |
| 内存使用 | `free -m` 或 `docker stats` | > 14G（80% of 16G） |
| 磁盘 | `df -h /` | > 85% |
| 会话数 | `curl /broker/stats` | 异常激增 |

### 13.2 简易告警脚本（腾讯云可对接云监控 CM）

```bash
cat > /usr/local/bin/hermes-monitor.sh <<'EOF'
#!/bin/bash
HEALTH=$(curl -sf http://127.0.0.1:8080/broker/health)
MEM=$(free -m | awk '/Mem:/{print $3}')
LIMIT=14000

if [ -z "$HEALTH" ]; then
    # 触发告警（邮件/短信/微信 — 接你的通道）
    echo "ALERT: broker down on $(date)" | mail -s "broker down" admin@yourdomain.com
fi
if [ "$MEM" -gt "$LIMIT" ]; then
    echo "ALERT: memory high $MEM MB on $(date)" | mail -s "mem high" admin@yourdomain.com
fi
EOF
chmod +x /usr/local/bin/hermes-monitor.sh
echo "*/5 * * * * root /usr/local/bin/hermes-monitor.sh" > /etc/cron.d/hermes-monitor
```

更专业的方案：腾讯云 Prometheus 监控 + 告警（ARMS），但需要 broker 暴露 `/metrics` 端点（评审文档 P0）。

---

## 14. 完整时间表（迁移日 Day 0）

| 阶段 | 时间 | 操作 | 风险点 |
|---|---|---|---|
| T-3d | 30min | 申请 SSL 证书（DNSPod 解析临时指向 CVM 验证） | DNS TTL 太长切换慢 |
| T-3d | 1h | CVM 初始化（装 docker、clone 代码） | 无 |
| T-2d | 2h | 数据迁移 tar 包 + 推送镜像到 TCR | **传输安全**（tar 含密钥） |
| T-1d | 30min | docker compose up -d + 容器验证 | 镜像或配置问题 |
| T-1d | 30min | 宿主机 nginx + certbot | 证书路径 |
| T-0 | 5min | DNS 切换主域解析 | **5 分钟内可回滚** |
| T-0 | 1h | 观察日志 + 测试功能 | 性能/兼容性 |
| T+1d | 30min | **轮换所有密钥** | 操作繁琐但必要 |
| T+7d | 持续 | 观察稳定性 + 设置监控告警 | — |

**总投入**：约 1-2 人天。

---

## 15. 回滚方案（**写在前面的安全网**）

任何时刻（切流前 + 切流后），如果新 CVM 出问题：

```bash
# DNS 切回旧 IP（5 分钟内生效，取决于 TTL）
# DNSPod 控制台 → 修改 A 记录回旧 IP
```

**前提**：
1. 旧机器的 broker **没关**（如果之前因"停 broker 让数据一致"关了，先起回去）
2. 旧机器的数据没动（迁移是单向拷，不破坏源）

回滚时间 = DNS TTL（建议 5 分钟）。

---

## 16. 风险与缓解

| 风险 | 缓解 |
|---|---|
| tar 包传输泄露（tar 含 auth.json/.env 明文） | §11 强制轮换密钥；传输走 scp（加密） |
| 镜像 3.28G 拉/构建慢 | 推 TCR 走内网（§4.3）；或本地 `docker save` 后 scp |
| 数据迁移中现网有写入会丢 | 切流前**先停现网 broker**（`docker compose stop` 或 `kill`），迁移完起 ECS |
| 单 CVM 单点 | 跟现网同样问题；彻底解决仍要 k8s（评审支柱 1-3） |
| certbot 续期失败 | 加监控；用腾讯云免费证书可避免 |
| 容器内 broker 与宿主机 nginx 双层风险 | 容器 nginx 出错 → broker 仍可访问（8080）；宿主机 nginx 出错 → 容器 nginx 不受影响 |

---

## 17. 与现有文档的关系

- `docs/k8s-migration-plan.md`：完整 K8s 化方案（本文是其降级版）
- `docs/architecture-review.md`：评审（生产内核差距；本文已部分用 limits/资源限制落实 P0）
- `docs/k8s-base-image-plan.md`：基础镜像方案（本文用的就是其基础镜像方案 v2）
- 本文：**腾讯云单机落地手册**

---

## 18. 速查清单（贴在工位）

```
□ CVM 4C16G 系统盘 100G Ubuntu 22.04
□ 安全组: 入站 22/80/443
□ DNSPod 解析 → CVM IP
□ SSL 证书就绪 (fullchain.pem + privkey.pem)
□ ssh root@<CVM_IP>
□ apt update && apt install -y nginx certbot rsync curl
□ curl -fsSL https://get.docker.com | bash
□ apt install -y docker-compose-plugin
□ (可选) TCR 个人版 + docker login + docker push
□ scp 现网 hermes-migrate.tar.gz → CVM
□ tar -xzf hermes-migrate.tar.gz -C /
□ git clone 仓库 + git checkout feat/k8s-phase1-lift-and-shift
□ bash k8s/prep-build-context.sh
□ docker build -f docker/Dockerfile -t hermes-platform:v1 .
□ 写 docker-compose.yml（resources.limits.memory: 12G）
□ docker compose up -d
□ 写 /etc/nginx/sites-available/hermes（SSL 终结 + 转发）
□ nginx -t && systemctl reload nginx
□ curl /broker/health 验证
□ DNS 切流（主域）
□ 24h 观察
□ 轮换 GitHub OAuth secret、openclaw router key、其他 model key
□ 设 cron 备份到 COS
□ 设 cron 健康检查 + 告警
```

---

## 19. 下一步

需要我**继续做哪个**？
1. 帮你写 `docker-compose.yml` 完整版（直接可用）
2. 写 `nginx/hermes.conf` 完整版（宿主机 nginx 配置）
3. 写迁移脚本 `migrate-to-tencent.sh`（一键执行大部分步骤）
4. 把当前分支里现网 Dockerfile 的基础镜像从私有镜像切到 Docker Hub（这样代码同时兼容腾讯云/阿里云/本地，零差异）