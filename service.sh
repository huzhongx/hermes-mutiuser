#!/usr/bin/env bash
# 先不取版本号
# TAG=${1}

# 本地临时调试 先固定写死版本号
TAG=latest
SERVICE_NAME=pro-service-hermes-mutiuser
DOCKERFILE=docker/Dockerfile
IMAGE="qx-images.tencentcloudcr.com/qunxing/"${SERVICE_NAME}:${TAG}

# ── 准备 agent：COPY 现网「已打补丁 + 已建 venv」的运行态，而非 git clone ──
# 原因：git clone 出来的 agent 没有现成的 venv（591 个依赖包）也没打 patches，
# 镜像里 broker spawn 的 /root/.hermes/hermes-agent/venv/bin/python 会缺失 → 全部用户无法用。
# 方案 A（lift-and-shift）：直接拷现网运行态，镜像 = 现网忠实副本。
AGENT_SRC="${HERMES_AGENT_SRC:-/root/.hermes/hermes-agent}"
AGENT_CTX="./hermes-agent"

if [ ! -x "${AGENT_SRC}/venv/bin/python" ]; then
    echo "ABORT: 现网 agent venv 不存在或损坏：${AGENT_SRC}/venv/bin/python" >&2
    echo "       broker spawn agent 需要该解释器，镜像不能缺。" >&2
    exit 1
fi

echo "==> 同步现网 agent（含 venv + 已应用补丁）到构建上下文 ${AGENT_CTX}"
rm -rf "${AGENT_CTX}"
# 排除运行不需要的重型目录：.git、node_modules(UI 构建)、tests、website、__pycache__
# 保留 venv(1.6G,运行必需) + 全部源码(含已打补丁) + skills/tools/plugins 等
rsync -a \
    --exclude='/.git' \
    --exclude='/node_modules' \
    --exclude='/tests' \
    --exclude='/website' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    "${AGENT_SRC}/" "${AGENT_CTX}/"

echo "==> agent 就绪：${AGENT_CTX}/venv/bin/python"
echo "    （如运行时发现 UI/TUI 构建产物缺失，去掉上面 --exclude='/node_modules' 重跑）"

docker build -f ${DOCKERFILE} --pull . -t ${IMAGE}
echo "${IMAGE}"

# 先不push
# docker push ${IMAGE}

# 本地临时测试
docker rm -f hermes-mutiuser 2>/dev/null
docker run -itd -p 8080:8080 -p 80:80 -p 443:443 --name hermes-mutiuser ${IMAGE}
# docker ps -a
# docker exec -it hermes-mutiuser bash

