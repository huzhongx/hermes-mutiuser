#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_DIR"

echo "=== 1. 启动 Docker 服务（Redis + PostgreSQL）==="
docker compose up -d

echo "等待服务就绪..."
sleep 5

# 检查 PostgreSQL 是否就绪
for i in $(seq 1 30); do
    if docker exec hermes_postgres pg_isready -U hermes -d hermes_platform > /dev/null 2>&1; then
        echo "PostgreSQL 已就绪"
        break
    fi
    echo "等待 PostgreSQL... ($i/30)"
    sleep 2
done

# 检查 Redis 是否就绪
for i in $(seq 1 30); do
    if docker exec hermes_redis redis-cli -a redis_secret_2026 ping > /dev/null 2>&1; then
        echo "Redis 已就绪"
        break
    fi
    echo "等待 Redis... ($i/30)"
    sleep 2
done

echo ""
echo "=== 2. 安装 Python 依赖 ==="
pip install -r requirements.txt -q

echo ""
echo "=== 3. 启动 Platform API Server ==="
echo "访问 http://localhost:8080/api/v1/health 验证"
echo ""
python api_server.py
