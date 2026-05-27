#!/bin/bash
set -e

BASE_URL="http://localhost:8080"
ALPHA_KEY="sk-alp-haaa0001"
BETA_KEY="sk-bet-hbbb0002"

echo "=== 1. 健康检查 ==="
curl -s "$BASE_URL/api/v1/health" | python3 -m json.tool 2>/dev/null || curl -s "$BASE_URL/api/v1/health"

echo ""
echo "=== 2. 创建租户 Alpha 会话 ==="
resp=$(curl -s -X POST "$BASE_URL/api/v1/sessions" \
  -H "X-API-Key: $ALPHA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"nous-hermes-3"}')
echo "$resp" | python3 -m json.tool 2>/dev/null || echo "$resp"
SESSION_ID=$(echo "$resp" | python3 -c "import sys,json; print(json.load(sys.stdin).get('session_id',''))" 2>/dev/null)
echo "Session ID: $SESSION_ID"

if [ -z "$SESSION_ID" ] || [ "$SESSION_ID" = "None" ]; then
    echo "创建会话失败，跳过后续测试"
    exit 1
fi

echo ""
echo "=== 3. 发送消息 ==="
curl -s -X POST "$BASE_URL/api/v1/sessions/$SESSION_ID/messages" \
  -H "X-API-Key: $ALPHA_KEY" \
  -H "Content-Type: application/json" \
  -d '{"message":"Hello, what is 2+2?"}' | python3 -m json.tool 2>/dev/null || echo "消息发送失败（可能 Hermes 未安装）"

echo ""
echo "=== 4. 查询会话 ==="
curl -s "$BASE_URL/api/v1/sessions/$SESSION_ID" \
  -H "X-API-Key: $ALPHA_KEY" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "=== 5. 列出租户会话 ==="
curl -s "$BASE_URL/api/v1/sessions" \
  -H "X-API-Key: $ALPHA_KEY" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "=== 6. 平台统计 ==="
curl -s "$BASE_URL/api/v1/stats" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "=== 7. 跨租户访问测试（应返回 403）==="
curl -s "$BASE_URL/api/v1/sessions/$SESSION_ID" \
  -H "X-API-Key: $BETA_KEY" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "=== 8. 限流测试（快速发送多个请求）==="
for i in $(seq 1 3); do
    echo "请求 $i:"
    curl -s -o /dev/null -w "  HTTP %{http_code}\n" "$BASE_URL/api/v1/sessions" \
      -H "X-API-Key: $ALPHA_KEY"
done

echo ""
echo "=== 9. 无效 API Key 测试（应返回 401）==="
curl -s "$BASE_URL/api/v1/sessions" \
  -H "X-API-Key: invalid-key" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "=== 10. 关闭会话 ==="
curl -s -X DELETE "$BASE_URL/api/v1/sessions/$SESSION_ID" \
  -H "X-API-Key: $ALPHA_KEY" | python3 -m json.tool 2>/dev/null || echo "$resp"

echo ""
echo "测试完成"
