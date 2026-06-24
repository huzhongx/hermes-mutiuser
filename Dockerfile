# ──────────────────────────────────────────────────────────────────────────
# Hermes Platform — 阶段一 lift-and-shift 镜像
# 单镜像含：broker + per-user Hermes agent 运行时 + nginx（pod 内 sidecar）
# 设计原则：不修改 hermes_broker.py；本机单机版与 k8s 版本完全隔离。
#
# 构建前置：先执行 k8s/prep-build-context.sh，它在 k8s/build/ 下准备好：
#   k8s/build/hermes-agent/      现网已打补丁的 agent（含 venv）
#   k8s/build/hermes-home-base/  全局 skills / cache / 共享 json 缓存
# ──────────────────────────────────────────────────────────────────────────

FROM python:3.10-slim AS runtime

# ── 系统依赖：nginx（pod 内动态端口路由）+ git（agent 运行时可能用）+ 工具 ──
RUN apt-get update && apt-get install -y --no-install-recommends \
        nginx \
        git \
        ca-certificates \
        curl \
        procps \
        iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    && rm -f /etc/nginx/sites-enabled/default

# ── Python 依赖（镜像专用完整集合，含 pyjwt/requests）──
WORKDIR /opt/hermes-platform
COPY k8s/requirements-image.txt /tmp/requirements-image.txt
RUN pip install --no-cache-dir -r /tmp/requirements-image.txt

# ── Hermes Agent 运行时（现网已打补丁版本，含 venv）──
# 3.7G → prep 脚本已剔除 node_modules/tests/website/.git/__pycache__，约降至 ~1.5G
COPY k8s/build/hermes-agent /root/.hermes/hermes-agent

# ── 全局 HERMES_HOME 基线（只读：skills/cache/pairing/共享 json）──
# config.yaml 与 auth.json 不在此处 —— 运行时由 ConfigMap/Secret 挂载覆盖
COPY k8s/build/hermes-home-base /root/.hermes

# ── 补丁留档（provenance，运行不需要，便于溯源 agent 版本）──
COPY patches /opt/hermes-platform-patches

# ── broker 代码 ──
COPY . /opt/hermes-platform

# ── pod 内 nginx 配置（动态端口路由 + Origin 伪造）──
COPY k8s/nginx-pod.conf /etc/nginx/conf.d/hermes-pod.conf
# nginx http 块需要 $connection_upgrade map（conf.d 在 http 上下文，可直接声明）
RUN echo 'map $http_upgrade $connection_upgrade { default upgrade; "" close; }' \
        > /etc/nginx/conf.d/00-map-upgrade.conf

# broker 作为 uvicorn 启动时 import hermes_broker，工作目录即代码目录
WORKDIR /opt/hermes-platform
ENV PYTHONUNBUFFERED=1 \
    HERMES_HOME=/root/.hermes \
    HERMES_AGENT=/root/.hermes/hermes-agent \
    PYTHONDONTWRITEBYTECODE=1

# pod 暴露：nginx:80（Ingress 入口）。broker:8080 与 9119-9200 仅 pod 内回环。
EXPOSE 80

# 无统一 ENTRYPOINT —— 由 StatefulSet 的两个容器分别指定 command：
#   nginx 容器： nginx -g 'daemon off;'
#   broker 容器： uvicorn hermes_broker:app --host 0.0.0.0 --port 8080 ...
