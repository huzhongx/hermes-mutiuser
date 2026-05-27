"""
Hermes Platform API Server — v3.0

基于 FastAPI + WebSocket 实现的多租户 REST API 服务。

v3.0 核心设计：
- 权限校验：Redis miss 时 fallback 查 PG
- 租户级限流：Redis INCR + 滑动窗口
- 事务安全：创建失败时完整回滚
- 统一错误处理中间件

端点：
  POST   /api/v1/sessions                  创建新会话
  POST   /api/v1/sessions/{id}/messages    发送消息
  GET    /api/v1/sessions/{id}              查询会话状态
  DELETE /api/v1/sessions/{id}              关闭会话
  GET    /api/v1/sessions                  列出租户会话
  GET    /api/v1/health                    健康检查
  GET    /api/v1/stats                     平台统计
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import asyncpg
import redis.asyncio as aioredis
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from process_manager import ProcessManager
from ws_pool import WSConnectionPool

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# 限流配置
# ─────────────────────────────────────────────────────────

RATE_LIMIT_RPM = 60
RATE_LIMIT_WINDOW = 60

# ─────────────────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────────────────

pm: Optional[ProcessManager] = None
ws_pool: Optional[WSConnectionPool] = None
pg_pool: Optional[asyncpg.Pool] = None
redis_pool: Optional[aioredis.Redis] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pm, ws_pool, pg_pool, redis_pool

    # PostgreSQL 连接池
    pg_pool = await asyncpg.create_pool(
        host="127.0.0.1",
        port=5432,
        user="hermes",
        password="hermes_secret_2026",
        database="hermes_platform",
        min_size=5,
        max_size=20,
    )

    # Redis 连接
    redis_pool = aioredis.from_url(
        "redis://:redis_secret_2026@127.0.0.1:6379/0",
        decode_responses=True,
    )

    # 从 PG 同步租户配额
    rows = await pg_pool.fetch(
        "SELECT id, max_sessions FROM tenants WHERE is_active = TRUE"
    )
    tenant_quotas = {str(r["id"]): r["max_sessions"] for r in rows}

    # 进程管理器
    pm = ProcessManager(
        sessions_root="/tmp/hermes_sessions",
        tenant_quotas=tenant_quotas,
    )
    await pm.start()

    # WS 连接池
    ws_pool = WSConnectionPool()

    logger.info("Platform Gateway 启动完成")
    yield

    # Shutdown
    await ws_pool.close_all()
    await pm.stop()
    await redis_pool.close()
    await pg_pool.close()
    logger.info("Platform Gateway 已关闭")


app = FastAPI(title="Hermes Platform API", lifespan=lifespan)


# ─────────────────────────────────────────────────────────
# 统一错误处理中间件
# ─────────────────────────────────────────────────────────

@app.middleware("http")
async def error_handler(request: Request, call_next):
    start = time.monotonic()
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception(f"未捕获异常: {request.method} {request.url.path}")
        return JSONResponse(
            {"detail": "内部服务错误", "error": str(e)},
            status_code=500,
        )
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        logger.info(
            f"{request.method} {request.url.path} "
            f"→ {response.status_code} ({latency_ms}ms)"
        )
    return response


# ─────────────────────────────────────────────────────────
# Pydantic 模型
# ─────────────────────────────────────────────────────────

class CreateSessionRequest(BaseModel):
    model: str = "nous-hermes-3"
    system_prompt: Optional[str] = None


class SendMessageRequest(BaseModel):
    message: str = Field(..., min_length=1)
    timeout: int = 120


class SessionResponse(BaseModel):
    session_id: str
    tenant_id: str
    status: str
    hermes_home: str
    created_at: str


# ─────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(...)) -> uuid.UUID:
    """验证 API Key，返回 tenant_id。Redis miss 时 fallback 查 PG。"""
    # 1. 查 Redis
    tenant = await redis_pool.hgetall(f"tenant:{x_api_key}")
    if tenant:
        return uuid.UUID(tenant["id"])

    # 2. Redis miss → 查 PG
    row = await pg_pool.fetchrow(
        "SELECT id, name, max_sessions FROM tenants "
        "WHERE api_key = $1 AND is_active = TRUE",
        x_api_key,
    )
    if not row:
        raise HTTPException(status_code=401, detail="无效的 API Key")

    # 3. 写入 Redis 缓存
    await redis_pool.hset(
        f"tenant:{x_api_key}",
        mapping={"id": str(row["id"]), "name": row["name"]},
    )
    await redis_pool.expire(f"tenant:{x_api_key}", 3600)

    # 4. 同步配额到 ProcessManager
    pm.set_tenant_quota(str(row["id"]), row["max_sessions"])

    return row["id"]


async def verify_session_access(
    session_id: str, tenant_id: uuid.UUID
) -> dict:
    """
    验证租户对 session 的访问权限。
    Redis miss 时 fallback 查 PG。
    """
    # 1. 查 Redis
    session_data = await redis_pool.hgetall(f"session:{session_id}")
    if session_data and session_data.get("tenant_id") == str(tenant_id):
        return session_data

    # 2. Redis miss → 查 PG
    try:
        sid_uuid = uuid.UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="无效的 session_id 格式")

    row = await pg_pool.fetchrow(
        "SELECT tenant_id, hermes_home, hermes_port, status FROM sessions "
        "WHERE id = $1 AND status != 'closed'",
        sid_uuid,
    )
    if not row:
        raise HTTPException(
            status_code=404, detail=f"会话 {session_id} 未找到或已关闭"
        )
    if row["tenant_id"] != tenant_id:
        raise HTTPException(status_code=403, detail="无权访问此会话")

    # 3. 回写 Redis
    session_data = {
        "tenant_id": str(row["tenant_id"]),
        "hermes_home": row["hermes_home"],
        "hermes_port": str(row["hermes_port"]),
    }
    await redis_pool.hset(f"session:{session_id}", mapping=session_data)
    # session 路由不设 TTL，关闭时主动删除

    return session_data


async def check_rate_limit(tenant_id: uuid.UUID):
    """租户级限流"""
    key = f"ratelimit:{tenant_id}"
    count = await redis_pool.incr(key)
    if count == 1:
        await redis_pool.expire(key, RATE_LIMIT_WINDOW)
    if count > RATE_LIMIT_RPM:
        raise HTTPException(
            status_code=429,
            detail=f"请求频率超限（{RATE_LIMIT_RPM} 请求/{RATE_LIMIT_WINDOW}秒）",
        )


# ─────────────────────────────────────────────────────────
# 端点实现
# ─────────────────────────────────────────────────────────

@app.post("/api/v1/sessions")
async def create_session(
    tenant_id: uuid.UUID = Depends(verify_api_key),
    body: CreateSessionRequest = CreateSessionRequest(),
):
    """创建新会话（事务安全）"""
    await check_rate_limit(tenant_id)
    session_id = str(uuid.uuid4())
    proc = None
    conn = None

    try:
        # 1. 启动 Hermes 进程（含预热池分配）
        proc = await pm.create_session(
            tenant_id=str(tenant_id),
            session_id=session_id,
            model=body.model,
            system_prompt=body.system_prompt,
        )

        # 2. 建立 WS 连接
        conn = await ws_pool.add(session_id, "127.0.0.1", proc.port, token=proc.ws_token)

        # 3. 调用 Hermes session.create
        result = await conn.call("session.create", {"model": body.model})
        hermes_session_id = result.get("session_id", session_id)

        # 4. 写入 PostgreSQL
        try:
            db_session_id = (
                uuid.UUID(hermes_session_id)
                if len(hermes_session_id) == 36
                else uuid.uuid4()
            )
        except ValueError:
            db_session_id = uuid.uuid4()

        await pg_pool.execute(
            """
            INSERT INTO sessions
                (id, tenant_id, status, hermes_home, hermes_port, pid, metadata)
            VALUES ($1, $2, 'active', $3, $4, $5, $6)
            """,
            db_session_id,
            tenant_id,
            proc.hermes_home,
            proc.port,
            proc.pid,
            {"model": body.model},
        )

        # 5. 写入 Redis 路由表（无 TTL，关闭时主动删除）
        await redis_pool.hset(
            f"session:{session_id}",
            mapping={
                "tenant_id": str(tenant_id),
                "hermes_port": str(proc.port),
                "hermes_home": proc.hermes_home,
            },
        )

        return {"session_id": hermes_session_id, "status": "active"}

    except Exception as e:
        # 事务回滚
        logger.error(f"[{session_id}] 创建会话失败，执行回滚: {e}")
        if conn:
            try:
                await ws_pool.remove(session_id)
            except Exception:
                pass
        if proc:
            try:
                await pm.close_session(session_id, reason="create_rollback")
            except Exception:
                pass
        try:
            await redis_pool.delete(f"session:{session_id}")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")


@app.post("/api/v1/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    tenant_id: uuid.UUID = Depends(verify_api_key),
):
    """向指定会话发送消息"""
    await check_rate_limit(tenant_id)
    await verify_session_access(session_id, tenant_id)

    conn = await ws_pool.get(session_id)
    if not conn:
        raise HTTPException(status_code=404, detail="会话 WS 连接不存在")

    try:
        result = await conn.call("prompt.submit", {"message": body.message})
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"会话连接异常: {e}")
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=f"会话响应超时: {e}")

    # 更新活跃时间
    proc = pm.get_process(session_id)
    if proc:
        proc.touch()

    try:
        await pg_pool.execute(
            "UPDATE sessions SET last_active_at = NOW() WHERE id = $1",
            uuid.UUID(session_id),
        )
    except Exception:
        pass  # 非关键路径

    return {"response": result}


@app.get("/api/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    tenant_id: uuid.UUID = Depends(verify_api_key),
):
    """查询会话状态"""
    session_data = await verify_session_access(session_id, tenant_id)

    proc = pm.get_process(session_id)
    return {
        "session_id": session_id,
        "status": proc.status if proc else "unknown",
        "hermes_home": session_data.get("hermes_home"),
        "hermes_port": session_data.get("hermes_port"),
    }


@app.delete("/api/v1/sessions/{session_id}")
async def close_session(
    session_id: str,
    tenant_id: uuid.UUID = Depends(verify_api_key),
):
    """关闭会话"""
    await verify_session_access(session_id, tenant_id)

    # 1. 先更新 DB
    try:
        await pg_pool.execute(
            "UPDATE sessions SET status = 'closed', closed_at = NOW() WHERE id = $1",
            uuid.UUID(session_id),
        )
    except Exception as e:
        logger.error(f"[{session_id}] 更新 DB 失败: {e}")

    # 2. 关闭 WS
    try:
        await ws_pool.remove(session_id)
    except Exception as e:
        logger.error(f"[{session_id}] 关闭 WS 失败: {e}")

    # 3. 关闭进程
    try:
        await pm.close_session(session_id, reason="user_request")
    except Exception as e:
        logger.error(f"[{session_id}] 关闭进程失败: {e}")

    # 4. 清 Redis
    await redis_pool.delete(f"session:{session_id}")

    return {"status": "closed"}


@app.get("/api/v1/sessions")
async def list_sessions(tenant_id: uuid.UUID = Depends(verify_api_key)):
    """列出租户所有会话"""
    rows = await pg_pool.fetch(
        "SELECT id, status, hermes_home, hermes_port, created_at, last_active_at "
        "FROM sessions WHERE tenant_id = $1 AND status != 'closed' "
        "ORDER BY created_at DESC",
        tenant_id,
    )
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/v1/health")
async def health():
    """健康检查"""
    try:
        await pg_pool.fetchval("SELECT 1")
        await redis_pool.ping()
        return {
            "status": "healthy",
            "postgres": "ok",
            "redis": "ok",
            "process_manager": pm.stats(),
            "ws_pool": ws_pool.stats(),
        }
    except Exception as e:
        return JSONResponse(
            {"status": "unhealthy", "error": str(e)},
            status_code=503,
        )


@app.get("/api/v1/stats")
async def stats():
    """平台统计"""
    return {
        "process_manager": pm.stats(),
        "ws_pool": ws_pool.stats(),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
