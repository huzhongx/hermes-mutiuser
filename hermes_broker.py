"""
Hermes Process Broker — 轻量级进程经纪人

对外只暴露一个简单 API：
  POST /broker/sessions          按用户分配 Hermes 进程，返回 WS 连接信息
  GET  /broker/sessions/{id}     查询进程状态
  DELETE /broker/sessions/{id}   回收进程
  GET  /broker/health            健康检查
  GET  /broker/stats             统计

设计原则：
  - 每个用户分配一个独立的 Hermes dashboard 进程
  - 返回 WS URL + token，调用方直接连 Hermes WS 通信
  - 不代理消息，不做消息转发
  - 空闲超时自动回收
  - 预热池减少冷启动延迟

启动：
  python3 hermes_broker.py
"""

import asyncio
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp
import jwt as pyjwt
import requests as req_lib
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────
# Hermes 进程管理
# ─────────────────────────────────────────────────────────

_HERMES_PYTHON = "/root/.hermes/hermes-agent/venv/bin/python"
_HERMES_MODULE = "hermes_cli.main"
_HERMES_CONFIG_HOME = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))

TOKEN_RE = re.compile(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"')


@dataclass
class HermesProcess:
    user_id: str
    session_id: str
    port: int
    work_dir: str
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    ws_token: str = ""
    status: str = "initializing"  # initializing / active / closing / closed
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    _log_fh = None

    def touch(self):
        self.last_active_at = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active_at


class ProcessBroker:
    def __init__(
        self,
        work_root: str = "/tmp/hermes_sessions",
        base_port: int = 9119,
        max_port: int = 9200,
        max_users: int = 80,
        idle_timeout: int = 1800,
        start_timeout: int = 90,
        warm_pool_size: int = 3,
        warm_pool_interval: int = 30,
    ):
        self.work_root = Path(work_root)
        self.base_port = base_port
        self.max_port = max_port
        self.max_users = max_users
        self.idle_timeout = idle_timeout
        self.start_timeout = start_timeout
        self.warm_pool_size = warm_pool_size
        self.warm_pool_interval = warm_pool_interval

        self._procs: Dict[str, HermesProcess] = {}  # user_id → process
        self._warm: Dict[str, HermesProcess] = {}    # session_id → process
        self._used_ports: Set[int] = set()
        self._free_ports: Set[int] = set(range(base_port, max_port + 1))
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._warm_task: Optional[asyncio.Task] = None

        self._public_host = os.environ.get("HERMES_PUBLIC_HOST", "127.0.0.1")

    def _alloc_port(self) -> int:
        if not self._free_ports:
            raise RuntimeError("无可用端口")
        port = min(self._free_ports)
        self._free_ports.discard(port)
        self._used_ports.add(port)
        return port

    def _free_port(self, port: int):
        self._used_ports.discard(port)
        self._free_ports.add(port)

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self):
        self.work_root.mkdir(parents=True, exist_ok=True)
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._warm_task = asyncio.create_task(self._warm_maintainer())
        logger.info(f"ProcessBroker started, warm_pool={self.warm_pool_size}")

    async def stop(self):
        for t in (self._cleanup_task, self._warm_task):
            if t:
                t.cancel()
        for sid in list(self._warm):
            await self._destroy_warm(sid)
        for uid in list(self._procs):
            await self.release(uid)
        logger.info("ProcessBroker stopped")

    # ── 核心接口：分配 ────────────────────────────────────

    async def acquire(self, user_id: str) -> dict:
        """
        为用户分配一个 Hermes 进程。
        如果已有活跃进程直接返回；否则从预热池取或冷启动。
        """
        # 已有活跃进程 → 直接返回
        async with self._lock:
            existing = self._procs.get(user_id)
            if existing and existing.status == "active":
                existing.touch()
                return self._make_info(existing)

        # 容量检查
        async with self._lock:
            if len(self._procs) >= self.max_users:
                raise RuntimeError(f"已达最大用户数 {self.max_users}")

        # 尝试从预热池取
        proc = await self._take_warm(user_id)
        if proc is None:
            proc = await self._spawn(user_id)

        proc.status = "active"
        async with self._lock:
            self._procs[user_id] = proc

        return self._make_info(proc)

    async def release(self, user_id: str):
        async with self._lock:
            proc = self._procs.pop(user_id, None)
        if not proc:
            return
        await self._kill(proc)
        self._free_port(proc.port)
        # Only clean up the session work_dir (logs etc.), preserve hermes_home (state.db)
        if proc.work_dir and os.path.exists(proc.work_dir):
            # Move hermes_home aside if it's inside work_dir
            hermes_home = Path(proc.work_dir) / "hermes_home"
            if hermes_home.exists() and str(hermes_home).startswith(str(self.work_root)):
                # hermes_home is now at stable path, not inside work_dir — safe to remove work_dir
                pass
            shutil.rmtree(proc.work_dir, ignore_errors=True)

    def get(self, user_id: str) -> Optional[HermesProcess]:
        return self._procs.get(user_id)

    def _make_info(self, proc: HermesProcess) -> dict:
        # 优先使用 nginx 代理域名（环境变量配置）
        nginx_domain = os.environ.get("HERMES_NGINX_DOMAIN", "")
        if nginx_domain:
            ws_url = f"wss://{nginx_domain}/hermes/ws/{proc.port}/api/ws?token={proc.ws_token}"
            http_url = f"https://{nginx_domain}/hermes/dash/{proc.port}"
        else:
            ws_url = f"ws://{self._public_host}:{proc.port}/api/ws?token={proc.ws_token}"
            http_url = f"http://{self._public_host}:{proc.port}"
        return {
            "user_id": proc.user_id,
            "session_id": proc.session_id,
            "status": proc.status,
            "pid": proc.pid,
            "port": proc.port,
            "ws_url": ws_url,
            "http_url": http_url,
            "ws_token": proc.ws_token,
            "created_at": proc.created_at,
        }

    # ── 预热池 ────────────────────────────────────────────

    async def _take_warm(self, user_id: str) -> Optional[HermesProcess]:
        """Take a warm process, kill it, and respawn for the real user.

        Warm processes use a shared __warm__ HERMES_HOME which cannot be
        re-assigned to a different user without process isolation. We kill
        the warm process, free its resources, and let acquire() fall through
        to a fresh _spawn() for the real user.
        """
        async with self._lock:
            if not self._warm:
                return None
            sid, proc = next(iter(self._warm.items()))
            del self._warm[sid]

        # Kill warm process and free its resources
        await self._kill(proc)
        self._free_port(proc.port)
        if proc.work_dir and os.path.exists(proc.work_dir):
            shutil.rmtree(proc.work_dir, ignore_errors=True)

        logger.info(f"[{user_id}] 预热进程已回收，将冷启动新进程")
        return None

    async def _warm_maintainer(self):
        """Warm pool maintainer.

        NOTE: With per-user HERMES_HOME isolation, warm pool processes cannot
        be reused across users. The warm pool is effectively disabled — all
        processes are cold-started per user. Keeping the maintainer as a no-op
        for future optimization (e.g., pre-allocate ports or per-user warm pools).
        """
        pass

    async def _spawn_warm(self):
        sid = f"__warm_{uuid.uuid4().hex[:8]}__"
        try:
            proc = await self._spawn("__warm__", session_id=sid)
            proc.status = "warm"
            async with self._lock:
                self._warm[sid] = proc
            logger.info(f"[warm] 就绪 port={proc.port}, pool={len(self._warm)}")
        except Exception as e:
            logger.error(f"[warm] 启动失败: {e}")

    async def _destroy_warm(self, sid: str):
        async with self._lock:
            proc = self._warm.pop(sid, None)
        if not proc:
            return
        await self._kill(proc)
        self._free_port(proc.port)
        if proc.work_dir and os.path.exists(proc.work_dir):
            shutil.rmtree(proc.work_dir, ignore_errors=True)

    # ── 底层：启动进程 ────────────────────────────────────

    # Files symlinked into per-user HERMES_HOME (shared, read-only)
    _SHARED_SYMLINK_FILES = [
        "auth.json",
        ".env",
        "models_dev_cache.json",
        "ollama_cloud_models_cache.json",
    ]

    # Files copied into per-user HERMES_HOME (user-specific, mutable)
    _SHARED_COPY_FILES = [
        "config.yaml",
    ]

    async def _spawn(self, user_id: str, session_id: str = None) -> HermesProcess:
        async with self._lock:
            port = self._alloc_port()

        if session_id is None:
            session_id = str(uuid.uuid4())

        work_dir = self.work_root / (user_id if user_id != "__warm__" else "__warm__") / session_id
        work_dir.mkdir(parents=True, exist_ok=True)
        logs_dir = work_dir / "logs"
        logs_dir.mkdir(exist_ok=True)

        # Stable per-user HERMES_HOME (survives reconnects)
        # Path: {work_root}/{user_id}/hermes_home
        user_home = self.work_root / (user_id if user_id != "__warm__" else "__warm__") / "hermes_home"
        user_home.mkdir(parents=True, exist_ok=True)

        # Copy user-specific config files (each user has independent settings)
        for fname in self._SHARED_COPY_FILES:
            src = _HERMES_CONFIG_HOME / fname
            dst = user_home / fname
            if src.exists() and not (dst.exists() or dst.is_symlink()):
                shutil.copy2(str(src), str(dst))

        # Symlink shared config files from the global HERMES_HOME
        for fname in self._SHARED_SYMLINK_FILES:
            src = _HERMES_CONFIG_HOME / fname
            dst = user_home / fname
            if src.exists() and not (dst.exists() or dst.is_symlink()):
                os.symlink(src, dst)

        # Symlink shared directories (read-only, shared across users)
        for dname in ("cache", "pairing"):
            src = _HERMES_CONFIG_HOME / dname
            dst = user_home / dname
            if src.is_dir() and not (dst.exists() or dst.is_symlink()):
                os.symlink(src, dst)

        # Copy per-user directories (mutable, user-specific)
        # skills: each user manages their own installed skills independently
        for dname in ("skills",):
            src = _HERMES_CONFIG_HOME / dname
            dst = user_home / dname
            if src.is_dir() and not (dst.exists() or dst.is_symlink()):
                shutil.copytree(str(src), str(dst))

        # Symlink shared files (skill snapshots etc.)
        for fname in (".skills_prompt_snapshot.json",):
            src = _HERMES_CONFIG_HOME / fname
            dst = user_home / fname
            if src.exists() and not (dst.exists() or dst.is_symlink()):
                os.symlink(src, dst)

        proc = HermesProcess(
            user_id=user_id,
            session_id=session_id,
            port=port,
            work_dir=str(work_dir),
        )

        log_fh = None
        try:
            log_fh = open(logs_dir / "session.log", "a")

            proc_env = os.environ.copy()
            proc_env["HERMES_HOME"] = str(user_home)

            logger.info(f"[{user_id}] 启动 dashboard port={port}")
            proc.process = await asyncio.create_subprocess_exec(
                _HERMES_PYTHON, "-m", _HERMES_MODULE,
                "dashboard", "--tui", "--skip-build",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--no-open",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=proc_env,
                cwd=str(work_dir),
            )
            proc.pid = proc.process.pid
            proc._log_fh = log_fh
            log_fh = None

            # 等待就绪 + 提取 token
            await asyncio.wait_for(self._wait_ready(proc), timeout=self.start_timeout)
            logger.info(f"[{user_id}] 就绪 port={port}")

        except Exception as e:
            logger.error(f"[{user_id}] 启动失败: {e}")
            await self._kill(proc)
            self._free_port(port)
            if work_dir.exists():
                shutil.rmtree(work_dir, ignore_errors=True)
            if log_fh:
                log_fh.close()
            raise

        return proc

    async def _wait_ready(self, proc: HermesProcess):
        url = f"http://127.0.0.1:{proc.port}"
        for _ in range(45):
            await asyncio.sleep(2)
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(url + "/", timeout=aiohttp.ClientTimeout(total=5)) as resp:
                        if resp.status != 200:
                            continue
                        html = await resp.text()
                        m = TOKEN_RE.search(html)
                        if m:
                            proc.ws_token = m.group(1)
                            return
            except Exception:
                continue
        raise RuntimeError("dashboard 启动超时")

    async def _kill(self, proc: HermesProcess):
        p = proc.process
        if p is None or p.returncode is not None:
            return
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), timeout=5)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                p.kill()
            except ProcessLookupError:
                pass
        if proc._log_fh:
            try:
                proc._log_fh.close()
            except Exception:
                pass
        logger.info(f"[{proc.user_id}] 进程 PID={proc.pid} 已终止")

    # ── 后台清理 ──────────────────────────────────────────

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                to_close = [
                    uid for uid, p in self._procs.items()
                    if p.status == "active" and now - p.last_active_at > self.idle_timeout
                ]
                for uid in to_close:
                    logger.info(f"[{uid}] 空闲超时 {self.idle_timeout}s，回收")
                    await self.release(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理异常: {e}")

    def stats(self) -> dict:
        return {
            "active_users": len(self._procs),
            "warm_pool": len(self._warm),
            "free_ports": len(self._free_ports),
            "max_users": self.max_users,
            "public_host": self._public_host,
        }


# ─────────────────────────────────────────────────────────
# GitHub OAuth & JWT Session
# ─────────────────────────────────────────────────────────

_GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
_GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
_JWT_SECRET = secrets.token_hex(32)
_JWT_ALG = "HS256"
_JWT_EXP_SECONDS = 7 * 24 * 3600  # 7 days
_OAUTH_STATE_STORE: Dict[str, float] = {}  # state → created_at, in-memory


def _oauth_enabled() -> bool:
    return bool(_GITHUB_CLIENT_ID and _GITHUB_CLIENT_SECRET)


def _create_jwt(github_login: str, name: str = "", avatar_url: str = "") -> str:
    payload = {
        "sub": github_login,
        "name": name or github_login,
        "avatar": avatar_url,
        "exp": time.time() + _JWT_EXP_SECONDS,
    }
    return pyjwt.encode(payload, _JWT_SECRET, algorithm=_JWT_ALG)


def _verify_jwt(token: str) -> Optional[dict]:
    try:
        payload = pyjwt.decode(token, _JWT_SECRET, algorithms=[_JWT_ALG])
        return {"sub": payload["sub"], "name": payload.get("name", ""), "avatar": payload.get("avatar", "")}
    except Exception:
        return None


def _get_session_user(request: Request) -> Optional[dict]:
    """Extract user info from hermes_session cookie."""
    token = request.cookies.get("hermes_session")
    if not token:
        return None
    return _verify_jwt(token)


def _require_session_user(request: Request) -> dict:
    """Extract user info or raise 401."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# ─────────────────────────────────────────────────────────
# FastAPI
# ─────────────────────────────────────────────────────────

broker: Optional[ProcessBroker] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global broker
    broker = ProcessBroker()
    await broker.start()
    yield
    await broker.stop()


app = FastAPI(title="Hermes Process Broker", lifespan=lifespan)


class AcquireRequest(BaseModel):
    user_id: Optional[str] = ""


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.monotonic()
    response = None
    try:
        response = await call_next(request)
    except Exception as e:
        logger.exception(f"异常: {request.method} {request.url.path}")
        response = JSONResponse({"detail": str(e)}, status_code=500)
    finally:
        ms = int((time.monotonic() - start) * 1000)
        status = response.status_code if response else 500
        logger.info(f"{request.method} {request.url.path} → {status} ({ms}ms)")
    return response


# ── OAuth endpoints ────────────────────────────────────────

@app.get("/auth/github")
async def github_login(request: Request):
    """Redirect to GitHub OAuth authorize page."""
    if not _oauth_enabled():
        raise HTTPException(status_code=500, detail="GitHub OAuth not configured")
    state = secrets.token_urlsafe(32)
    _OAUTH_STATE_STORE[state] = time.time()
    # Clean old states (>10 min)
    now = time.time()
    expired = [k for k, v in _OAUTH_STATE_STORE.items() if now - v > 600]
    for k in expired:
        _OAUTH_STATE_STORE.pop(k, None)

    nginx_domain = os.environ.get("HERMES_NGINX_DOMAIN", "")
    if nginx_domain:
        redirect_uri = f"https://{nginx_domain}/auth/callback"
    else:
        redirect_uri = str(request.base_url.replace(path="/auth/callback"))
    url = (
        f"https://github.com/login/oauth/authorize"
        f"?client_id={_GITHUB_CLIENT_ID}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=user:email"
        f"&state={state}"
    )
    return RedirectResponse(url)


@app.get("/auth/callback")
async def github_callback(code: str, state: str):
    """Handle GitHub OAuth callback — exchange code for user info, issue JWT."""
    # Validate state
    if state not in _OAUTH_STATE_STORE:
        return JSONResponse({"detail": "Invalid OAuth state"}, status_code=400)
    _OAUTH_STATE_STORE.pop(state, None)

    # Exchange code → access_token
    try:
        r = req_lib.post(
            "https://github.com/login/oauth/access_token",
            json={"client_id": _GITHUB_CLIENT_ID, "client_secret": _GITHUB_CLIENT_SECRET, "code": code},
            headers={"Accept": "application/json"},
            timeout=10,
        )
        r.raise_for_status()
        token_data = r.json()
        access_token = token_data.get("access_token")
        if not access_token:
            raise RuntimeError(f"No access_token: {token_data}")
    except Exception as e:
        logger.error(f"GitHub token exchange failed: {e}")
        return JSONResponse({"detail": "GitHub auth failed"}, status_code=502)

    # Get user info
    try:
        u = req_lib.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
            timeout=10,
        )
        u.raise_for_status()
        user_info = u.json()
        github_login = user_info.get("login")
        name = user_info.get("name") or github_login
        avatar_url = user_info.get("avatar_url", "")
        if not github_login:
            raise RuntimeError("No login in GitHub user response")
    except Exception as e:
        logger.error(f"GitHub user info failed: {e}")
        return JSONResponse({"detail": "Failed to get user info"}, status_code=502)

    # Issue JWT
    jwt_token = _create_jwt(github_login, name, avatar_url)
    logger.info(f"GitHub login: {github_login} ({name})")

    # Redirect to /chat with session cookie
    resp = RedirectResponse(url="/chat", status_code=302)
    resp.set_cookie(
        key="hermes_session",
        value=jwt_token,
        max_age=_JWT_EXP_SECONDS,
        httponly=True,
        secure=bool(os.environ.get("HERMES_NGINX_DOMAIN")),
        samesite="lax",
    )
    return resp


@app.get("/auth/user")
async def auth_user(request: Request):
    """Return current user info from session cookie (for frontend)."""
    user = _get_session_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


@app.post("/auth/logout")
async def auth_logout():
    """Clear session cookie."""
    resp = JSONResponse({"status": "logged_out"})
    resp.delete_cookie(key="hermes_session")
    return resp


# ── Broker endpoints (OAuth optional) ──────────────────────
# OAuth: if JWT cookie is present → use it as user_id
# No cookie → fall back to body.user_id (legacy / direct API)

@app.post("/broker/sessions")
async def acquire_session(request: Request, body: AcquireRequest = None):
    """为用户分配 Hermes 进程，返回 WS 连接信息"""
    user_id = None
    # Prefer JWT cookie (chat.html OAuth flow)
    user = _get_session_user(request)
    if user:
        user_id = user["sub"]
    elif body and body.user_id:
        user_id = body.user_id
    else:
        raise HTTPException(status_code=400, detail="user_id required")

    try:
        info = await broker.acquire(user_id)
        return info
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/broker/sessions/{user_id}")
async def get_session(user_id: str):
    proc = broker.get(user_id)
    if not proc:
        raise HTTPException(status_code=404, detail="用户无活跃会话")
    proc.touch()
    return broker._make_info(proc)


@app.delete("/broker/sessions/{user_id}")
async def release_session(user_id: str):
    proc = broker.get(user_id)
    if not proc:
        raise HTTPException(status_code=404, detail="用户无活跃会话")
    await broker.release(user_id)
    return {"status": "released", "user_id": user_id}


@app.post("/broker/upload")
async def upload_file(request: Request):
    """Upload a file to the user's process work directory. Returns the saved path."""
    user_id = None
    user = _get_session_user(request)
    if user:
        user_id = user["sub"]
    else:
        # Try query param or header for legacy mode
        user_id = request.query_params.get("user_id") or request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id required")

    proc = broker.get(user_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No active session")

    content_type = request.headers.get("content-type", "")
    if not content_type.startswith("multipart/form-data"):
        raise HTTPException(status_code=400, detail="multipart/form-data required")

    form = await request.form()
    upload = form.get("file")
    if not upload or not upload.filename:
        raise HTTPException(status_code=400, detail="file field required")

    # Save to process work_dir / uploads
    uploads_dir = Path(proc.work_dir) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename
    safe_name = re.sub(r'[^\w.\-]', '_', upload.filename)
    dest = uploads_dir / safe_name
    content = await upload.read()
    dest.write_bytes(content)

    logger.info(f"[{user_id}] File uploaded: {dest} ({len(content)} bytes)")
    return {"path": str(dest), "name": safe_name, "size": len(content)}


@app.get("/broker/health")
async def health():
    return {"status": "ok", **broker.stats()}


@app.get("/broker/stats")
async def stats():
    return broker.stats()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("hermes_broker:app", host="0.0.0.0", port=8080, log_level="info")
