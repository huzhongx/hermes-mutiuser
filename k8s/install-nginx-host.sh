#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# 安装宿主机 nginx 配置（SSL 终结 + 转发到容器）
# 详见 docs/tencent-cloud-deploy.md §8 + k8s/nginx-host.conf.template
#
# 用法：
#   sudo HERMES_DOMAIN=hermes.example.com \
#        HERMES_UPSTREAM_PORT=8080 \
#        bash k8s/install-nginx-host.sh
#
# 流程：
#   1. 替换模板里的 <YOUR_DOMAIN> / 端口占位符
#   2. 检测 SSL 证书（无则用 certbot 申请）
#   3. 安装配置 + $connection_upgrade map
#   4. nginx -t 校验 + systemctl reload
#   5. 验证 https://<domain>/broker/health
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

DOMAIN="${HERMES_DOMAIN:-}"
UPSTREAM_PORT="${HERMES_UPSTREAM_PORT:-8080}"
EMAIL="${CERTBOT_EMAIL:-admin@${DOMAIN#hermes.}}"

if [ -z "$DOMAIN" ]; then
    echo "ERROR: 必须设置 HERMES_DOMAIN（如 HERMES_DOMAIN=hermes.example.com）"
    exit 1
fi
if [ "$(id -u)" != 0 ]; then
    echo "ERROR: 必须 root 跑"
    exit 1
fi

TEMPLATE="/opt/hermes-platform/k8s/nginx-host.conf.template"
[ -f "$TEMPLATE" ] || { echo "ERROR: 模板 $TEMPLATE 不存在"; exit 1; }

# 1. 替换占位符
TMP=$(mktemp)
sed -e "s|<YOUR_DOMAIN>|$DOMAIN|g" \
    -e "s|server 127.0.0.1:8080;|server 127.0.0.1:$UPSTREAM_PORT;|g" \
    "$TEMPLATE" > "$TMP"

# 2. SSL：检查 / 申请
CERT_DIR="/etc/letsencrypt/live/$DOMAIN"
if [ ! -f "$CERT_DIR/fullchain.pem" ]; then
    echo "==> 证书不存在，启动 certbot..."
    apt-get install -y certbot >/dev/null
    # 需要先停 nginx 让 80 端口空闲（或用 standalone）
    systemctl stop nginx 2>/dev/null || true
    certbot certonly --standalone \
        -d "$DOMAIN" \
        --email "$EMAIL" \
        --agree-tos \
        --no-eff-email
    systemctl start nginx 2>/dev/null || true
fi
[ -f "$CERT_DIR/fullchain.pem" ] || { echo "ERROR: 证书申请失败"; exit 1; }
echo "✓ 证书就位: $CERT_DIR/fullchain.pem"

# 3. 安装 nginx 配置
echo "==> 安装 nginx 配置..."
cat > /etc/nginx/sites-available/hermes <<EOF
$(sed -e '/^# ──.*SSL/,/^\(listen 443\)/!d' \
       -e 's|<YOUR_DOMAIN>|$DOMAIN|g' \
       -e "s|server 127.0.0.1:8080;|server 127.0.0.1:$UPSTREAM_PORT;|g" \
       "$TMP")
EOF
# 实际上更直接的做法：把整个替换过的文件拷过去
sed -e "s|<YOUR_DOMAIN>|$DOMAIN|g" \
    -e "s|server 127.0.0.1:8080;|server 127.0.0.1:$UPSTREAM_PORT;|g" \
    "$TEMPLATE" > /etc/nginx/sites-available/hermes

# 安装 $connection_upgrade map
cat > /etc/nginx/conf.d/00-upgrade-map.conf <<'EOF'
map $http_upgrade $connection_upgrade {
    default upgrade;
    ""      close;
}
EOF

# 启用（注意：先删 default site 避免冲突）
ln -sf /etc/nginx/sites-available/hermes /etc/nginx/sites-enabled/hermes
rm -f /etc/nginx/sites-enabled/default

# 4. 校验 + reload
echo "==> 校验 nginx 配置..."
nginx -t

echo "==> reload nginx..."
systemctl reload nginx
echo "✓ nginx reloaded"

# 5. 验证
echo
echo "==> 验证 HTTPS 端到端..."
sleep 2
HEALTH=$(curl -sf --max-time 5 "https://$DOMAIN/broker/health" 2>&1 || echo "FAIL")
echo "  https://$DOMAIN/broker/health → $HEALTH"

rm -f "$TMP"
echo
echo "✅ 安装完成"
echo
echo "后续:"
echo "  - 证书自动续期: certbot 已配 systemd timer (默认)"
echo "  - 日志查看:     sudo tail -f /var/log/nginx/error.log"
echo "  - 重新生成配置: 重新跑本脚本（幂等）"