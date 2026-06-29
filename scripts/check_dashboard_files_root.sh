#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────
# 安全回归:检查每个活跃 hermes dashboard 进程的 managed-files root 是否锁定。
#
# 背景:hermes dashboard 的 /api/files/download?path= 在 locked_root=None 时
# 是任意文件读取漏洞(任何登录用户可读 /etc/passwd、broker .env 等)。
# broker 必须给每个进程注入 HERMES_DASHBOARD_FILES_ROOT={user_root} 才能堵住。
#
# 本脚本对每个活跃 dashboard 进程做两项检查:
#   1. 环境变量 HERMES_DASHBOARD_FILES_ROOT 已注入,且指向该用户的 user_root
#   2. 实测越权访问 /etc/passwd → 必须返回 403
# 任何一项失败即报错(退出码 1),适合放进 CI / 定期巡检。
#
# 用法:bash scripts/check_dashboard_files_root.sh
# ──────────────────────────────────────────────────────────────────────────
set -uo pipefail

PASS=0
FAIL=0
TOKEN_RE='__HERMES_SESSION_TOKEN__="([^"]+)"'

if ! command -v curl >/dev/null 2>&1; then
  echo "ABORT: curl not found" >&2; exit 2
fi

# 枚举活跃 dashboard 进程: pid → port
mapfile -t PROCS < <(ps -eo pid,cmd 2>/dev/null \
  | grep 'hermes_cli.main dashboard' | grep -v grep \
  | awk '{for(i=1;i<=NF;i++) if($i=="--port"){print $1, $(i+1); break}}')

if [ "${#PROCS[@]}" -eq 0 ]; then
  echo "⚠️  没有活跃的 hermes dashboard 进程(无法检查)。先 acquire 一个用户再跑。"
  exit 0
fi

for line in "${PROCS[@]}"; do
  pid="${line%% *}"
  port="${line##* }"

  # 进程 cwd = user_base (broker 启动时设的)
  user_base="$(readlink "/proc/$pid/cwd" 2>/dev/null)"
  if [ -z "$user_base" ]; then
    echo "❌ pid $pid port $port: 无法读取 cwd(进程已退出?)"
    FAIL=$((FAIL+1)); continue
  fi

  # 检查 1: HERMES_DASHBOARD_FILES_ROOT 已注入且 = user_base
  injected="$(tr '\0' '\n' < "/proc/$pid/environ" 2>/dev/null \
    | awk -F= '$1=="HERMES_DASHBOARD_FILES_ROOT"{print $2}')"
  if [ -z "$injected" ]; then
    echo "❌ pid $pid port $port: HERMES_DASHBOARD_FILES_ROOT 未注入 → dashboard 文件接口是任意文件读取漏洞"
    FAIL=$((FAIL+1)); continue
  fi
  if [ "$injected" != "$user_base" ]; then
    echo "❌ pid $pid port $port: locked_root($injected) ≠ user_base($user_base) → 锁定范围异常"
    FAIL=$((FAIL+1)); continue
  fi

  # 取 token (从 dashboard 首页)
  tok="$(curl -s --max-time 4 "http://127.0.0.1:$port/" 2>/dev/null \
    | grep -oE "$TOKEN_RE" | head -1 | sed -E 's/.*"([^"]+)".*/\1/')"
  if [ -z "$tok" ]; then
    echo "⚠️  pid $pid port $port: 取不到 token(跳过越权实测)"
    continue
  fi

  # 检查 2: 实测越权 /etc/passwd → 必须非 200
  code="$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 \
    "http://127.0.0.1:$port/api/files/download?token=$tok&path=/etc/passwd")"
  if [ "$code" = "200" ]; then
    echo "❌ pid $pid port $port: /etc/passwd 返回 200 → 越权读取漏洞!"
    FAIL=$((FAIL+1)); continue
  fi

  echo "✅ pid $pid port $port: locked_root=$injected, /etc/passwd→$code"
  PASS=$((PASS+1))
done

echo ""
echo "结果: $PASS 通过 / $FAIL 失败"
[ "$FAIL" -eq 0 ]
