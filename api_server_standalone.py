"""
Hermes Platform — Standalone Mode (无 PG/Redis 依赖)

用于开发和测试环境，用内存数据结构替代 PostgreSQL 和 Redis。
所有 API 端点正常工作，但数据不持久化。

启动方式：
  python api_server_standalone.py
"""

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Dict, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from process_manager import ProcessManager
from ws_pool import WSConnectionPool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

RATE_LIMIT_RPM = 60
RATE_LIMIT_WINDOW = 60


# ─────────────────────────────────────────────────────────
# 内存存储（替代 PG/Redis）
# ─────────────────────────────────────────────────────────

class MemoryStore:
    """模拟 Redis 的内存 KV 存储"""

    def __init__(self):
        self._data: Dict[str, dict] = {}
        self._ttls: Dict[str, float] = {}

    async def hset(self, key: str, *, mapping: dict):
        self._data[key] = {**self._data.get(key, {}), **mapping}
        self._ttls.pop(key, None)

    async def hgetall(self, key: str) -> dict:
        if key in self._ttls and time.time() > self._ttls[key]:
            self._data.pop(key, None)
            self._ttls.pop(key, None)
            return {}
        return dict(self._data.get(key, {}))

    async def delete(self, key: str):
        self._data.pop(key, None)
        self._ttls.pop(key, None)

    async def expire(self, key: str, seconds: int):
        if key in self._data:
            self._ttls[key] = time.time() + seconds

    async def incr(self, key: str) -> int:
        val = int(self._data.get(key, {}).get("_counter", 0)) + 1
        if key not in self._data:
            self._data[key] = {}
        self._data[key]["_counter"] = val
        return val

    async def ping(self):
        return True


class MemoryPG:
    """模拟 PostgreSQL 的内存存储"""

    def __init__(self):
        self._tenants: Dict[str, dict] = {}
        self._sessions: Dict[str, dict] = {}

    async def fetch(self, query: str, *args):
        if "tenants" in query and "is_active" in query:
            return [
                {"id": k, "name": v["name"], "max_sessions": v["max_sessions"]}
                for k, v in self._tenants.items()
                if v.get("is_active")
            ]
        if "sessions" in query and "tenant_id" in query and "status" in query:
            tid = str(args[0]) if args else None
            return [
                v for v in self._sessions.values()
                if v.get("tenant_id") == tid and v.get("status") != "closed"
            ]
        return []

    async def fetchrow(self, query: str, *args):
        if "tenants" in query and "api_key" in query:
            api_key = args[0] if args else None
            for v in self._tenants.values():
                if v.get("api_key") == api_key and v.get("is_active"):
                    return v
            return None
        if "sessions" in query and "WHERE id" in query:
            sid = str(args[0]) if args else None
            return self._sessions.get(sid)
        return None

    async def fetchval(self, query: str, *args):
        return 1

    async def execute(self, query: str, *args):
        if "INSERT INTO sessions" in query:
            sid = str(args[0])
            self._sessions[sid] = {
                "id": sid,
                "tenant_id": str(args[1]),
                "status": "active",
                "hermes_home": args[3],
                "hermes_port": args[4],
            }
        elif "UPDATE sessions SET status = 'closed'" in query:
            sid = str(args[-1])
            if sid in self._sessions:
                self._sessions[sid]["status"] = "closed"
        elif "UPDATE sessions SET last_active_at" in query:
            sid = str(args[-1])
            if sid in self._sessions:
                self._sessions[sid]["last_active_at"] = time.time()

    def seed_tenants(self):
        """预置测试租户"""
        alpha_id = str(uuid.uuid4())
        beta_id = str(uuid.uuid4())
        self._tenants[alpha_id] = {
            "id": alpha_id,
            "name": "tenant_alpha",
            "api_key": "sk-alp-haaa0001",
            "plan": "pro",
            "max_sessions": 20,
            "is_active": True,
        }
        self._tenants[beta_id] = {
            "id": beta_id,
            "name": "tenant_beta",
            "api_key": "sk-bet-hbbb0002",
            "plan": "basic",
            "max_sessions": 5,
            "is_active": True,
        }
        return {alpha_id: 20, beta_id: 5}


# ─────────────────────────────────────────────────────────
# 全局状态
# ─────────────────────────────────────────────────────────

pm: Optional[ProcessManager] = None
ws_pool: Optional[WSConnectionPool] = None
pg: Optional[MemoryPG] = None
store: Optional[MemoryStore] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global pm, ws_pool, pg, store

    pg = MemoryPG()
    tenant_quotas = pg.seed_tenants()

    store = MemoryStore()

    pm = ProcessManager(
        sessions_root="/tmp/hermes_sessions",
        tenant_quotas=tenant_quotas,
    )
    await pm.start()

    ws_pool = WSConnectionPool()

    logger.info("Platform Gateway (standalone) 启动完成")
    logger.info("测试 API Keys:")
    for tid, t in pg._tenants.items():
        logger.info(f"  {t['name']}: {t['api_key']} (max_sessions={t['max_sessions']})")
    yield

    await ws_pool.close_all()
    await pm.stop()
    logger.info("Platform Gateway 已关闭")


app = FastAPI(title="Hermes Platform API (Standalone)", lifespan=lifespan)


@app.middleware("http")
async def error_handler(request: Request, call_next):
    start = time.monotonic()
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception(f"未捕获异常: {request.method} {request.url.path}")
        response = JSONResponse(
            {"detail": "内部服务错误", "error": str(e)},
            status_code=500,
        )
    finally:
        latency_ms = int((time.monotonic() - start) * 1000)
        status = response.status_code if response else 500
        logger.info(
            f"{request.method} {request.url.path} "
            f"→ {status} ({latency_ms}ms)"
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


# ─────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────

async def verify_api_key(x_api_key: str = Header(...)) -> uuid.UUID:
    tenant = await store.hgetall(f"tenant:{x_api_key}")
    if tenant:
        return uuid.UUID(tenant["id"])

    for tid, t in pg._tenants.items():
        if t.get("api_key") == x_api_key and t.get("is_active"):
            await store.hset(
                f"tenant:{x_api_key}",
                mapping={"id": tid, "name": t["name"]},
            )
            await store.expire(f"tenant:{x_api_key}", 3600)
            pm.set_tenant_quota(tid, t["max_sessions"])
            return uuid.UUID(tid)

    raise HTTPException(status_code=401, detail="无效的 API Key")


async def verify_session_access(session_id: str, tenant_id: uuid.UUID) -> dict:
    session_data = await store.hgetall(f"session:{session_id}")
    if session_data and session_data.get("tenant_id") == str(tenant_id):
        return session_data

    session = pg._sessions.get(session_id)
    if not session or session.get("status") == "closed":
        raise HTTPException(status_code=404, detail=f"会话未找到或已关闭")
    if session.get("tenant_id") != str(tenant_id):
        raise HTTPException(status_code=403, detail="无权访问此会话")

    session_data = {
        "tenant_id": session["tenant_id"],
        "hermes_home": session.get("hermes_home", ""),
        "hermes_port": str(session.get("hermes_port", 0)),
    }
    await store.hset(f"session:{session_id}", mapping=session_data)
    return session_data


async def check_rate_limit(tenant_id: uuid.UUID):
    key = f"ratelimit:{tenant_id}"
    count = await store.incr(key)
    if count == 1:
        await store.expire(key, RATE_LIMIT_WINDOW)
    if count > RATE_LIMIT_RPM:
        raise HTTPException(status_code=429, detail="请求频率超限")


# ─────────────────────────────────────────────────────────
# 端点
# ─────────────────────────────────────────────────────────

@app.post("/api/v1/sessions")
async def create_session(
    tenant_id: uuid.UUID = Depends(verify_api_key),
    body: CreateSessionRequest = CreateSessionRequest(),
):
    await check_rate_limit(tenant_id)
    session_id = str(uuid.uuid4())

    try:
        proc = await pm.create_session(
            tenant_id=str(tenant_id),
            session_id=session_id,
            model=body.model,
        )

        conn = await ws_pool.add(session_id, "127.0.0.1", proc.port, token=proc.ws_token)
        result = await conn.call("session.create", {"model": body.model})

        hermes_session_id = result.get("session_id", session_id)
        logger.info(f"[{session_id}] Hermes session created: {hermes_session_id}")

        # Store using platform session_id as primary key
        await pg.execute(
            "INSERT INTO sessions",  # marker for mock
            session_id,
            tenant_id,
            "active",
            proc.hermes_home,
            proc.port,
            proc.pid,
            {"model": body.model, "hermes_session_id": hermes_session_id},
        )

        await store.hset(
            f"session:{session_id}",
            mapping={
                "tenant_id": str(tenant_id),
                "hermes_port": str(proc.port),
                "hermes_home": proc.hermes_home,
                "hermes_session_id": hermes_session_id,
            },
        )

        return {"session_id": session_id, "status": "active"}

    except Exception as e:
        logger.error(f"[{session_id}] 创建会话失败: {e}")
        try:
            await ws_pool.remove(session_id)
        except Exception:
            pass
        try:
            await pm.close_session(session_id, reason="create_rollback")
        except Exception:
            pass
        try:
            await store.delete(f"session:{session_id}")
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"创建会话失败: {e}")


@app.post("/api/v1/sessions/{session_id}/messages")
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    tenant_id: uuid.UUID = Depends(verify_api_key),
):
    await check_rate_limit(tenant_id)
    await verify_session_access(session_id, tenant_id)

    conn = await ws_pool.get(session_id)
    if not conn:
        raise HTTPException(status_code=404, detail="会话连接不存在")

    try:
        # Get Hermes session ID from store
        session_data = await store.hgetall(f"session:{session_id}")
        hermes_sid = session_data.get("hermes_session_id", session_id)
        result = await conn.call("prompt.submit", {
            "session_id": hermes_sid,
            "text": body.message,
        })
    except ConnectionError as e:
        raise HTTPException(status_code=503, detail=f"连接异常: {e}")
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=f"响应超时: {e}")

    proc = pm.get_process(session_id)
    if proc:
        proc.touch()

    return {"response": result}


@app.get("/api/v1/sessions/{session_id}")
async def get_session(
    session_id: str,
    tenant_id: uuid.UUID = Depends(verify_api_key),
):
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
    await verify_session_access(session_id, tenant_id)

    await pg.execute("UPDATE sessions SET status = 'closed'", session_id)
    await ws_pool.remove(session_id)
    await pm.close_session(session_id, reason="user_request")
    await store.delete(f"session:{session_id}")

    return {"status": "closed"}


@app.get("/api/v1/sessions")
async def list_sessions(tenant_id: uuid.UUID = Depends(verify_api_key)):
    rows = await pg.fetch(
        "SELECT * FROM sessions WHERE tenant_id = $1 AND status != 'closed'",
        tenant_id,
    )
    return {"sessions": [dict(r) for r in rows]}


@app.get("/api/v1/health")
async def health():
    return {
        "status": "healthy",
        "mode": "standalone (no PG/Redis)",
        "process_manager": pm.stats(),
        "ws_pool": ws_pool.stats(),
    }


@app.get("/api/v1/stats")
async def stats():
    return {
        "process_manager": pm.stats(),
        "ws_pool": ws_pool.stats(),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "api_server_standalone:app",
        host="0.0.0.0",
        port=8080,
        reload=False,
        log_level="info",
    )
