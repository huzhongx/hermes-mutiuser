#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# Pod entrypoint (阶段一 lift-and-shift)：nginx sidecar + broker 前台
#
# 设计要点（与 k8s/31-hermes-statefulset.yaml 对齐）：
#   - nginx 后台起，做 pod 内动态端口路由 + WS Origin 伪造
#   - broker 用 uvicorn 后台起，带 --timeout-graceful-shutdown=50
#     < StatefulSet 的 terminationGracePeriodSeconds(60)，给 broker.stop()
#     flush 全部 per-user state.db 留足时间
#   - shell 保持 PID 1，wait 两个进程；trap 在退出时终止 nginx，不留孤儿
#   - 任一关键进程挂掉则整体退出，交给 kubelet 重启 pod
# ──────────────────────────────────────────────────────────────────────────
set -euo pipefail

# 环境：k8s 通过 envFrom 注入 ConfigMap + Secret，无需 source .env。
# PYTHONUNBUFFERED 让 broker 日志实时输出到 k8s 日志聚合。
export PYTHONUNBUFFERED=1

WORKDIR="/opt/hermes-platform"
cd "$WORKDIR"

NGINX_PID=""

cleanup() {
    # 退出前停掉 nginx sidecar，避免孤儿进程
    if [ -n "$NGINX_PID" ] && kill -0 "$NGINX_PID" 2>/dev/null; then
        kill -TERM "$NGINX_PID" 2>/dev/null || true
        wait "$NGINX_PID" 2>/dev/null || true
    fi
}
trap cleanup EXIT INT TERM

echo "[entrypoint] starting nginx sidecar..."
nginx -g 'daemon off;' &
NGINX_PID=$!

# 等 nginx 起来再接流（简单轮询 80 端口）
for i in $(seq 1 20); do
    if curl -sf -o /dev/null "http://127.0.0.1:80/" 2>/dev/null \
       || ss -tln 2>/dev/null | grep -q ':80 '; then
        echo "[entrypoint] nginx ready (pid $NGINX_PID)"
        break
    fi
    sleep 0.25
done

# broker 后台起；shell 保持 PID 1，wait broker，这样 trap 能在退出时清理 nginx。
# --timeout-graceful-shutdown=50 配合 StatefulSet terminationGracePeriodSeconds=60：
#   SIGTERM → uvicorn 停止接流 → ≤50s lifespan 收尾（broker.stop flush 全部子进程）
#   → broker 退出 → wait 返回 → trap cleanup 终止 nginx → pod 退出。
#   若 nginx 先挂，broker 仍在但无法接流 → 也整体退出让 kubelet 重启。
echo "[entrypoint] starting broker (uvicorn)..."
uvicorn hermes_broker:app \
    --host 0.0.0.0 \
    --port 8080 \
    --timeout-graceful-shutdown 50 &
BROKER_PID=$!

# 任一关键进程退出则整体退出（交 kubelet 重启 pod）。
wait -n "$NGINX_PID" "$BROKER_PID"
EXIT_CODE=$?
echo "[entrypoint] a process exited (code=$EXIT_CODE), shutting down..."
exit "$EXIT_CODE"
