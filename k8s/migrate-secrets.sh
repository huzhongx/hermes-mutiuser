#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# 把现网 /root/.hermes 的三个 secrets 文件 scp 到 CVM
# （镜像里不含 secrets，必须从宿主机挂载；这是全新 CVM 部署的必要步骤）
#
# 在【现网机器】上跑（不是 CVM）：
#   bash k8s/migrate-secrets.sh <CVM_IP> [CVM_SSH_USER]
#
# 例：
#   bash k8s/migrate-secrets.sh 43.160.236.160
#   bash k8s/migrate-secrets.sh root@1.2.3.4 root
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

CVM_TARGET="${1:?用法: bash $0 <CVM_IP> [SSH_USER]}"
SSH_USER="${2:-root}"

# 解析 user@host 形式
if [[ "$CVM_TARGET" == *@* ]]; then
    SSH_USER="${CVM_TARGET%%@*}"
    CVM_IP="${CVM_TARGET#*@}"
else
    CVM_IP="$CVM_TARGET"
fi

SRC_DIR="/root/.hermes"
FILES=("auth.json" ".env" "config.yaml")

echo "==> 源（现网）: $SRC_DIR"
echo "==> 目标 CVM: ${SSH_USER}@${CVM_IP}"
echo "==> 文件: ${FILES[*]}"
echo

# 检查源文件
MISSING=0
for f in "${FILES[@]}"; do
    if [ ! -f "$SRC_DIR/$f" ]; then
        echo "  ✗ 缺失: $SRC_DIR/$f"
        MISSING=1
    else
        echo "  ✓ $f ($(wc -c < "$SRC_DIR/$f") bytes)"
    fi
done
[ "$MISSING" = "0" ] || { echo "源文件不全，abort"; exit 1; }

echo
echo "==> 在 CVM 创建 /root/.hermes 目录"
ssh "${SSH_USER}@${CVM_IP}" 'mkdir -p /root/.hermes && chmod 700 /root/.hermes'

echo
echo "==> scp 三个 secrets 文件（加密传输）"
for f in "${FILES[@]}"; do
    scp -q "$SRC_DIR/$f" "${SSH_USER}@${CVM_IP}:/root/.hermes/$f"
    echo "  ✓ $f 已传输"
done

echo
echo "==> CVM 端验证"
ssh "${SSH_USER}@${CVM_IP}" 'ls -la /root/.hermes/{auth.json,.env,config.yaml} 2>&1 && chmod 600 /root/.hermes/{auth.json,.env,config.yaml}'

echo
echo "✅ secrets 迁移完成"
echo
echo "⚠️  安全提醒："
echo "   1. 这三个文件含生产 API key / OAuth secret / 凭证池"
echo "   2. 建议迁移完成后轮换所有密钥（docs/tencent-cloud-deploy.md §11）"
echo "   3. CVM 上确保 /root/.hermes 权限 700，文件 600"
echo
echo "下一步：在 CVM 上 docker compose up -d"