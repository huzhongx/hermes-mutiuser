#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# 准备 Docker 构建上下文：把现网「正在运行」的 agent 与全局 HERMES_HOME 基线
# 暂存到 k8s/build/，供 Dockerfile COPY。
#
# 关键：直接拷贝「已打补丁、正在运行」的版本 —— 原样 lift，不在构建期重打补丁
# （避免重复 apply 报错）。patches/ 仍随镜像留档。
#
# 用法：
#   bash k8s/prep-build-context.sh            # 默认从 /root/.hermes 取
#   HERMES_HOME=/path bash k8s/prep-build-context.sh
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HERMES_HOME="${HERMES_HOME:-/root/.hermes}"
AGENT_SRC="${HERMES_HOME}/hermes-agent"
BUILD="${REPO_ROOT}/k8s/build"

echo "==> 源 HERMES_HOME : $HERMES_HOME"
echo "==> agent 源       : $AGENT_SRC"
echo "==> 构建暂存目录    : $BUILD"

[ -d "$AGENT_SRC" ] || { echo "ABORT: agent 源不存在：$AGENT_SRC"; exit 1; }
[ -x "${AGENT_SRC}/venv/bin/python" ] || { echo "WARN: agent venv python 不存在，镜像可能无法起 dashboard"; }

rm -rf "$BUILD"
mkdir -p "$BUILD/hermes-agent" "$BUILD/hermes-home-base"

# ── 1) agent：剔除运行不需要的重型目录，3.7G → ~1.5G ──
#   排除：node_modules(1.6G, UI 构建)、tests(25M)、website(27M)、.git、__pycache__
#   保留：venv(1.5G, 运行必需)、hermes_cli、agent、apps、scripts、配置等
echo "==> 拷贝 agent（排除 node_modules/tests/website/.git）..."
rsync -a \
  --exclude='/node_modules' \
  --exclude='/tests' \
  --exclude='/website' \
  --exclude='/.git' \
  --exclude='__pycache__' \
  --exclude='*.pyc' \
  "$AGENT_SRC/" "$BUILD/hermes-agent/"

# 回退：若运行时发现 UI 异常需 node_modules，去掉上面 --exclude='/node_modules' 重跑

# ── 2) 全局 HERMES_HOME 基线（只读共享部分）──
#   config.yaml / auth.json 不拷（由 ConfigMap/Secret 挂载）
echo "==> 拷贝 hermes-home 基线（skills/cache/pairing/共享 json）..."
for item in skills cache pairing models_dev_cache.json ollama_cloud_models_cache.json; do
  src="${HERMES_HOME}/${item}"
  if [ -e "$src" ]; then
    cp -a "$src" "$BUILD/hermes-home-base/"
  else
    echo "   跳过（不存在）：$item"
  fi
done

echo
echo "==> 完成。体积概览："
du -sh "$BUILD"/* 2>/dev/null || true
echo
echo "下一步："
echo "  docker build -t hermes-platform:phase1 ."
echo "（.dockerignore 已排除 .env/.jwt_secret/sessions，密钥由 k8s Secret 挂载）"
