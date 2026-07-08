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

# ── venv 的解释器：uv 管理的独立 python（3.11.15），必须一并 COPY 进镜像相同路径 ──
# venv/bin/python 是 symlink → /root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu/bin/python3.11
# pyvenv.cfg 的 home 用 cpython-3.11-...（指向 3.11.15 的 symlink）。
# 镜像基础是 python:3.11-slim（系统 python 不参与），venv 仍走这个 uv python，故必须搬过去。
UV_PY_SRC="${UV_PY_SRC:-/root/.local/share/uv/python/cpython-3.11.15-linux-x86_64-gnu}"
UV_PY_CTX="./uv-python/cpython-3.11.15-linux-x86_64-gnu"

if [ ! -x "${UV_PY_SRC}/bin/python3.11" ]; then
    echo "ABORT: uv python 不存在：${UV_PY_SRC}/bin/python3.11" >&2
    echo "       venv 的解释器靠它，镜像里没有则 agent 全部 import 失败。" >&2
    exit 1
fi
echo "==> 同步 uv python（venv 解释器，104M）到构建上下文 ${UV_PY_CTX}"
rm -rf ./uv-python
mkdir -p "${UV_PY_CTX}"
rsync -a "${UV_PY_SRC}/" "${UV_PY_CTX}/"

# ── 准备 hermes 全局只读资源（broker spawn 时软链源）──
# 必须含：skills/ cache/ pairing/ models_dev_cache.json ollama_cloud_models_cache.json
# 含认证：auth.json .env config.yaml（这些是敏感配置，生产用 k8s Secret 挂载覆盖，
#         本地测试时拷入能跑通完整链路）
HERMES_SRC="${HERMES_HOME_SRC:-/root/.hermes}"
HERMES_CTX="./hermes"

echo "==> 同步 /root/.hermes 全局只读资源到构建上下文 ${HERMES_CTX}"
rm -rf "${HERMES_CTX}"
mkdir -p "${HERMES_CTX}"

# 必须项（broker spawn 必需）
for item in skills cache pairing models_dev_cache.json ollama_cloud_models_cache.json; do
    src="${HERMES_SRC}/${item}"
    if [ -e "$src" ]; then
        cp -a "$src" "${HERMES_CTX}/"
    else
        echo "   跳过（不存在）：$item"
    fi
done

# 可选项：密钥/配置（拷贝让本地能跑通；生产用 k8s Secret 挂载覆盖）
# 设为 HERMES_INCLUDE_SECRETS=0 可跳过（k8s 部署场景）
if [ "${HERMES_INCLUDE_SECRETS:-1}" = "1" ]; then
    for item in auth.json .env config.yaml; do
        src="${HERMES_SRC}/${item}"
        if [ -e "$src" ]; then
            cp -a "$src" "${HERMES_CTX}/"
        fi
    done
else
    echo "   HERMES_INCLUDE_SECRETS=0，跳过 auth.json/.env/config.yaml（生产场景）"
fi

docker build -f ${DOCKERFILE} --pull . -t ${IMAGE}
echo "${IMAGE}"

# 先不push
# docker push ${IMAGE}

# 本地临时测试
docker rm -f hermes-mutiuser 2>/dev/null
docker run -itd -p 8080:8080 -p 80:80 -p 443:443 --name hermes-mutiuser ${IMAGE}
# docker ps -a
# docker exec -it hermes-mutiuser bash

