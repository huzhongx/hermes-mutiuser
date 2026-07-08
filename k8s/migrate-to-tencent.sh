#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# Hermes Platform → 腾讯云 CVM 一键迁移脚本
#
# 适用范围：单机 docker 部署（k8s 部署见 k8s/README.md）
# 详见 docs/tencent-cloud-deploy.md
#
# 用法：
#   ssh root@<新CVM_IP>
#   bash k8s/migrate-to-tencent.sh --help
#   bash k8s/migrate-to-tencent.sh --dry-run        # 演练，不做实际改动
#   bash k8s/migrate-to-tencent.sh                  # 实跑
#
# 设计原则：
#   - 幂等：已完成的步骤检测后跳过（用标记文件）
#   - dry-run：不修改任何状态，仅打印要做的事
#   - 失败可恢复：每个步骤独立，失败不破坏已完成步骤
#   - 前置检查：检测关键缺失（CPU/内存/Docker/网络）并 abort
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ─── 颜色 ───
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
log()  { echo -e "${BLUE}[$(date +%H:%M:%S)]${NC} $*"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
err()  { echo -e "${RED}✗${NC} $*"; }
die()  { err "$*"; exit 1; }

# ─── 配置（按需改）───
REPO_URL="${REPO_URL:-https://github.com/huzhongx/hermes-mutiuser.git}"  # 或 gitlab 仓库
BRANCH="${BRANCH:-feat/k8s-phase1-lift-and-shift}"
DOMAIN="${DOMAIN:-}"                          # 用 --domain 传
CVM_MEMORY_MIN_GB="${CVM_MEMORY_MIN_GB:-12}"   # 至少 12G，低于此 abort

STATE_DIR="/var/lib/hermes-migrate"
mkdir -p "$STATE_DIR"

DRY_RUN=0
SKIP_DATA_MIGRATE=0
SKIP_IMAGE_PULL=0
SKIP_COMPOSE_UP=0

# ─── 解析参数 ───
usage() {
cat <<EOF
用法: bash $(basename "$0") [选项]

选项:
  --dry-run              仅打印要做的事，不修改系统
  --domain <name>        部署域名（如 hermes.example.com），用于 HERMES_NGINX_DOMAIN
  --skip-data            跳过数据迁移（数据已通过其他方式 rsync）
  --skip-image           跳过镜像构建（假设镜像已存在或从 TCR 拉）
  --skip-compose-up      跳过 docker compose up -d（只准备，不启动）
  --repo <url>           Git 仓库 URL（默认 $REPO_URL）
  --branch <name>        Git 分支（默认 $BRANCH）
  --memory-min <gb>      最低内存要求 GB（默认 ${CVM_MEMORY_MIN_GB}）
  -h / --help            显示本帮助

EOF
exit 0
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run)            DRY_RUN=1; shift ;;
        --domain)             DOMAIN="$2"; shift 2 ;;
        --skip-data)          SKIP_DATA_MIGRATE=1; shift ;;
        --skip-image)         SKIP_IMAGE_PULL=1; shift ;;
        --skip-compose-up)    SKIP_COMPOSE_UP=1; shift ;;  # --skip-compose-up 形式
        --skip-compose-up)    SKIP_COMPOSE_UP=1; shift ;;
        --repo)               REPO_URL="$2"; shift 2 ;;
        --branch)             BRANCH="$2"; shift 2 ;;
        --memory-min)         CVM_MEMORY_MIN_GB="$2"; shift 2 ;;
        -h|--help)            usage ;;
        *) err "未知参数: $1"; usage ;;
    esac
done

# ─── dry-run 包装器 ───
run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo -e "${YELLOW}[dry-run]${NC} $*"
    else
        "$@"
    fi
}

# ─── 步骤标记（幂等）───
mark_done() { echo "$1" > "$STATE_DIR/$1.done"; }
is_done()   { [ -f "$STATE_DIR/$1.done" ]; }

# ─── 前置检查 ───
preflight() {
    log "========== 前置检查 =========="

    # 必须 root
    if [ "$(id -u)" != "0" ]; then
        die "必须以 root 跑"
    fi

    # 内存检查
    TOTAL_MEM_GB=$(free -g | awk '/Mem:/{print $2}')
    if [ "$TOTAL_MEM_GB" -lt "$CVM_MEMORY_MIN_GB" ]; then
        die "内存 ${TOTAL_MEM_GB}G < 最低要求 ${CVM_MEMORY_MIN_GB}G。请用 4C16G 或更大"
    fi
    ok "内存 ${TOTAL_MEM_GB}G >= ${CVM_MEMORY_MIN_GB}G"

    # OS 检查
    if [ ! -f /etc/os-release ]; then
        warn "无法检测 OS（无 /etc/os-release）"
    elif grep -qE 'Ubuntu (22|24)' /etc/os-release; then
        ok "OS: $(grep PRETTY_NAME /etc/os-release | cut -d= -f2)"
    else
        warn "OS 不是 Ubuntu 22/24，建议用 Ubuntu 22.04 LTS"
    fi

    # Docker
    if ! command -v docker >/dev/null 2>&1; then
        die "docker 未安装。请先 curl -fsSL https://get.docker.com | bash"
    fi
    DOCKER_VER=$(docker version --format '{{.Server.Version}}' 2>/dev/null || echo "")
    if [ -z "$DOCKER_VER" ]; then
        die "docker daemon 未运行"
    fi
    ok "docker $DOCKER_VER"

    # docker compose
    if ! docker compose version >/dev/null 2>&1; then
        die "docker compose plugin 未安装。apt install -y docker-compose-plugin"
    fi
    ok "docker compose $(docker compose version --short)"

    # 磁盘
    DISK_FREE=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
    if [ "$DISK_FREE" -lt 10 ]; then
        die "磁盘可用 ${DISK_FREE}G < 10G（镜像 3.3G + 数据 1.2G + 日志）"
    fi
    ok "磁盘可用 ${DISK_FREE}G"

    # 网络（ping 一个公网域名，不通就 warn 但不 abort）
    if ! curl -fsS --max-time 5 -o /dev/null https://www.baidu.com 2>/dev/null; then
        warn "无法访问 https://www.baidu.com（可能影响 docker pull / git clone）"
    else
        ok "网络可达"
    fi

    echo
    log "前置检查清单（人工确认）："
    echo "  [ ] 已准备好 DNS 解析 ${DOMAIN:-<未设置>:-填入域名} → CVM 公网 IP"
    echo "  [ ] 已申请 SSL 证书（或准备用 certbot 申请）"
    echo "  [ ] 已配置腾讯云安全组：入站 22/80/443"
    echo "  [ ] 已确认现网 broker 已停止（或准备在切换前停）"
    echo "  [ ] 已用 scp 把数据 tar 包传到 CVM 并解压"
    echo
    log "如以上都 ok，按 Enter 继续，Ctrl-C 取消"
    [ "$DRY_RUN" = "1" ] || read -r _
}

# ─── 步骤 1: 安装必要系统包 ───
step_system_init() {
    log "========== 步骤 1: 系统初始化 =========="
    if is_done step1; then ok "已跳过"; return; fi

    run apt-get update -qq
    run apt-get install -y -qq nginx certbot rsync curl jq htop
    run timedatectl set-timezone Asia/Shanghai 2>/dev/null || true

    mark_done step1
    ok "系统初始化完成"
}

# ─── 步骤 2: 克隆代码 + 切分支 ───
step_clone_repo() {
    log "========== 步骤 2: 克隆代码 =========="
    if is_done step2; then ok "已跳过"; return; fi

    if [ -d /opt/hermes-platform ]; then
        warn "/opt/hermes-platform 已存在，跳过 clone"
    else
        run bash -c "cd /opt && git clone $REPO_URL hermes-platform"
    fi

    cd /opt/hermes-platform
    run git fetch origin
    run git checkout "$BRANCH" 2>/dev/null || run git checkout -b "$BRANCH" "origin/$BRANCH"
    run git pull origin "$BRANCH" || true

    mark_done step2
    ok "代码已就位（branch: $BRANCH）"
}

# ─── 步骤 3: 数据迁移确认 ───
step_data_migrate() {
    log "========== 步骤 3: 数据迁移确认 =========="
    if [ "$SKIP_DATA_MIGRATE" = "1" ]; then ok "已跳过（--skip-data）"; return; fi

    if [ -d /tmp/hermes_sessions ] && [ -d /root/.hermes/hermes-agent ]; then
        ok "数据已就位（检测到 /tmp/hermes_sessions 和 /root/.hermes/hermes-agent）"
        warn "如果这是旧 CVM 但需要用最新数据，请手动 scp + tar"
        return
    fi

    warn "未检测到迁移数据。请确保已完成："
    echo "    1. 现网: tar -czf /tmp/hermes-migrate.tar.gz <...>"
    echo "    2. scp 传到 CVM: /root/hermes-migrate.tar.gz"
    echo "    3. tar -xzf /root/hermes-migrate.tar.gz -C /"
    echo "    4. shred -u /root/hermes-migrate.tar.gz  # 删除"
    die "数据未就位，请先手动迁移"
}

# ─── 步骤 4: 构建/拉取镜像 ───
step_build_image() {
    log "========== 步骤 4: 构建镜像（或从 TCR 拉）=========="
    if is_done step4; then ok "已跳过"; return; fi

    cd /opt/hermes-platform

    if [ "$DRY_RUN" = "1" ]; then
        echo -e "${YELLOW}[dry-run]${NC} docker pull 或 bash prep-build-context.sh + docker build"
        return
    fi

    # 优先：从 TCR 拉（推荐路径，省 1.7G build context）
    TCR_IMAGE="qx-images.tencentcloudcr.com/qunxing/hermes-platform:v1"
    if docker pull "$TCR_IMAGE" 2>&1 | tail -2; then
        docker tag "$TCR_IMAGE" hermes-platform:v1
        ok "从 TCR 拉取成功: $TCR_IMAGE"
    else
        warn "TCR 拉取失败（可能未登录或仓库不可达），回退到本地构建"

        # 检查基础镜像（Docker Hub 公开，应可拉取）
        docker pull python:3.11-slim

        run bash k8s/prep-build-context.sh

        log "构建 hermes-platform:v1 ...（约 2-3 分钟）"
        if docker build -f docker/Dockerfile -t hermes-platform:v1 . 2>&1 | tee /tmp/build.log | tail -5; then
            ok "镜像构建成功: $(docker images hermes-platform:v1 --format '{{.Size}}')"
        else
            err "构建失败，查看 /tmp/build.log"
            tail -30 /tmp/build.log
            die "请修复构建错误后重跑（此步骤会幂等跳过已完成的部分）"
        fi

        # 验证 agent 可起
        if docker run --rm --entrypoint /root/.hermes/hermes-agent/venv/bin/python hermes-platform:v1 \
            -c 'import hermes_cli.main; print("✓ agent 加载 OK")' 2>&1 | tail -1; then
            ok "agent 验证通过"
        else
            warn "agent 验证失败（不阻塞，可能是 import 警告）"
        fi

        # 清理 build context
        run rm -rf hermes-agent uv-python hermes
    fi

    mark_done step4
}

# ─── 步骤 5: 写 .env ───
step_env_file() {
    log "========== 步骤 5: 配置 .env =========="
    if is_done step5; then ok "已跳过"; return; fi

    cd /opt/hermes-platform

    if [ -z "$DOMAIN" ]; then
        err "未指定 --domain，无法写 .env"
        die "用法: $0 --domain hermes.example.com"
    fi

    if [ "$DRY_RUN" = "1" ]; then
        echo -e "${YELLOW}[dry-run]${NC} write .env with HERMES_NGINX_DOMAIN=$DOMAIN"
        return
    fi

    if [ -f .env ] && grep -q HERMES_NGINX_DOMAIN .env; then
        ok ".env 已存在且含 HERMES_NGINX_DOMAIN，跳过"
    else
        cat > .env <<EOF
# docker-compose 部署用（自动生成于 $(date -I)）
HERMES_NGINX_DOMAIN=$DOMAIN
EOF
        ok ".env 已写入"
    fi

    mark_done step5
}

# ─── 步骤 6: docker compose up ───
step_compose_up() {
    log "========== 步骤 6: docker compose up -d =========="
    if [ "$SKIP_COMPOSE_UP" = "1" ]; then ok "已跳过（--skip-compose-up）"; return; fi

    cd /opt/hermes-platform

    run docker compose up -d
    sleep 8

    if [ "$DRY_RUN" = "1" ]; then
        ok "dry-run 跳过验证"
        return
    fi

    # 验证
    if docker compose ps hermes | grep -q "Up"; then
        ok "容器已启动"
    else
        die "容器未启动，docker compose logs hermes"
    fi

    # 内部 broker health
    if docker exec hermes curl -fs http://127.0.0.1/broker/health > /dev/null 2>&1; then
        ok "broker health OK（通过容器内 nginx 反代）"
    elif docker exec hermes curl -fs http://127.0.0.1:8080/broker/health > /dev/null 2>&1; then
        warn "broker 直连 8080 通，但 80 (nginx) 不通"
    else
        err "broker 健康检查失败"
        docker compose logs --tail=20 hermes
        die "请排查"
    fi

    mark_done step6
}

# ─── 步骤 7: 宿主机 nginx + certbot（仅提示，配置因人而异）───
step_nginx_certbot() {
    log "========== 步骤 7: 宿主机 nginx + SSL =========="
    warn "此步骤需人工配置（看 docs/tencent-cloud-deploy.md §8-§9）:"
    echo "  1. 申请证书: certbot certonly --standalone -d $DOMAIN"
    echo "  2. 写 /etc/nginx/sites-available/hermes（参考 docs §8.1）"
    echo "  3. ln -sf ... /etc/nginx/sites-enabled/ && nginx -t && systemctl reload nginx"
    echo "  4. curl -I https://$DOMAIN/broker/health 验证"
    echo
    warn "DNS 必须先解析到本 CVM 公网 IP（DNSPod 控制台）"
}

# ─── 步骤 8: 后置提醒 ───
step_post() {
    log "========== 步骤 8: 必做收尾 =========="
    echo
    warn "🚨 必做：迁移后立刻轮换所有密钥（docs §11）："
    echo "    - GitHub OAuth client secret"
    echo "    - openclaw router API key (auth.json)"
    echo "    - 其他 model API key (.env)"
    echo
    warn "🚨 必做：DNS 切换前先双轨运行 24-48h"
    echo
    ok "迁移脚本完成。详细步骤 + 回滚: docs/tencent-cloud-deploy.md"
}

# ─── 主流程 ───
main() {
    echo
    log "===== Hermes Platform → 腾讯云 CVM 迁移 ====="
    log "DRY_RUN:    ${DRY_RUN}"
    log "REPO_URL:   ${REPO_URL}"
    log "BRANCH:     ${BRANCH}"
    log "DOMAIN:     ${DOMAIN:-<未设置>}"
    log "MEM_MIN:    ${CVM_MEMORY_MIN_GB}G"
    log "SKIP_DATA:  ${SKIP_DATA_MIGRATE}  SKIP_IMG: ${SKIP_IMAGE_PULL}  SKIP_UP: ${SKIP_COMPOSE_UP}"
    echo

    preflight
    step_system_init
    step_clone_repo
    step_data_migrate
    step_build_image
    step_env_file
    step_compose_up
    step_nginx_certbot
    step_post

    echo
    ok "✅ 迁移脚本执行完毕"
}

main "$@"