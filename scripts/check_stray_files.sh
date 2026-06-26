#!/bin/bash
# check_stray_files.sh — 检测 /tmp/hermes_sessions/ 下的越界写入文件
#
# 背景：agent 通过 shell 跑脚本时，HERMES_WRITE_SAFE_ROOT 不拦 shell，脚本
# 内部用绝对路径 open() 可能写到共享根目录。该脚本定期扫描，发现散落文件
# 时告警 + 记录（不自动删）。
#
# 用法：
#   ./scripts/check_stray_files.sh            # 打印告警
#   ./scripts/check_stray_files.sh --json     # 输出 JSON（供监控）
#   ./scripts/check_stray_files.sh --age 3600 # 只报 1 小时内新增的（秒）
#
# 放进 cron：
#   */10 * * * * /opt/hermes-platform/scripts/check_stray_files.sh >> /var/log/hermes_stray_files.log 2>&1

set -u

SESSIONS_ROOT="${SESSIONS_ROOT:-/tmp/hermes_sessions}"
JSON_OUT=0
MAX_AGE=0

while [ $# -gt 0 ]; do
    case "$1" in
        --json) JSON_OUT=1; shift ;;
        --age) MAX_AGE="${2:-0}"; shift 2 ;;
        *) shift ;;
    esac
done

[ -d "$SESSIONS_ROOT" ] || { echo "sessions root not found: $SESSIONS_ROOT" >&2; exit 0; }

# 散落文件 = 直接位于 $SESSIONS_ROOT 下的文件（正常情况根目录只应有用户子目录）
mapfile -t STRAYS < <(find "$SESSIONS_ROOT" -maxdepth 1 -type f 2>/dev/null)

# 按时间过滤
FILTERED=()
now=$(date +%s)
for f in "${STRAYS[@]}"; do
    if [ "$MAX_AGE" -gt 0 ]; then
        mtime=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        age=$((now - mtime))
        [ "$age" -gt "$MAX_AGE" ] && continue
    fi
    FILTERED+=("$f")
done

count=${#FILTERED[@]}

if [ "$JSON_OUT" -eq 1 ]; then
    # 简单 JSON 输出
    echo "{\"root\":\"$SESSIONS_ROOT\",\"stray_count\":$count,\"files\":["
    first=1
    for f in "${FILTERED[@]}"; do
        sz=$(stat -c %s "$f" 2>/dev/null || echo 0)
        mt=$(stat -c %Y "$f" 2>/dev/null || echo 0)
        name=$(basename "$f" | sed 's/"/\\"/g')
        [ $first -eq 1 ] || echo ","
        printf '  {"name":"%s","size":%s,"mtime":%s}' "$name" "$sz" "$mt"
        first=0
    done
    echo
    echo "]}"
    exit 0
fi

# 人类可读输出
if [ "$count" -eq 0 ]; then
    echo "[check_stray_files] OK: $SESSIONS_ROOT 根目录无散落文件"
    exit 0
fi

echo "[check_stray_files] ⚠️ 发现 $count 个散落文件（应为 0）:"
for f in "${FILTERED[@]}"; do
    sz=$(numfmt --to=iec --suffix=B "$(stat -c %s "$f" 2>/dev/null || echo 0)" 2>/dev/null || stat -c %s "$f")
    mt=$(date -d @"$(stat -c %Y "$f" 2>/dev/null || echo 0)" '+%Y-%m-%d %H:%M' 2>/dev/null)
    echo "  $mt  $sz  $f"
done
echo
echo "这些文件是技能脚本用绝对路径越界写入的。建议："
echo "  1. 归位到正确用户会话目录，或"
echo "  2. rm 直接删除（broker 不会自动清理根目录）"
exit 1
