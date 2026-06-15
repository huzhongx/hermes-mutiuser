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
import json
import logging
import os
import re
import secrets
import shutil
import zipfile
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp
import jwt as pyjwt
import requests as req_lib
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
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
        work_root: str = None,
        base_port: int = None,
        max_port: int = None,
        max_users: int = None,
        idle_timeout: int = None,
        start_timeout: int = 90,
        warm_pool_size: int = None,
        warm_pool_interval: int = 30,
    ):
        work_root = work_root or os.environ.get("SESSIONS_ROOT", "/tmp/hermes_sessions")
        base_port = base_port or int(os.environ.get("BASE_PORT", "9119"))
        max_port = max_port or int(os.environ.get("MAX_PORT", "9200"))
        max_users = max_users or int(os.environ.get("MAX_SESSIONS", "80"))
        idle_timeout = idle_timeout or int(os.environ.get("IDLE_TIMEOUT", "1800"))
        warm_pool_size = warm_pool_size or int(os.environ.get("WARM_POOL_SIZE", "3"))
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
        # Per-user locks to prevent the TOCTOU race in acquire() — see _user_locks docstring.
        self._user_locks: Dict[str, asyncio.Lock] = {}
        self._cleanup_task: Optional[asyncio.Task] = None
        self._warm_task: Optional[asyncio.Task] = None

        # Drain state
        self._draining: bool = False
        self._drain_event: Optional[asyncio.Event] = None
        self._drain_progress: Dict[str, str] = {}  # user_id → "pending"|"flushing"|"done"|"error"|"timeout"
        self._drain_task: Optional[asyncio.Task] = None
        self._drain_started_at: Optional[float] = None

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
        # Kill any leftover Hermes dashboard processes from previous broker runs
        await self._cleanup_stale_processes()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._warm_task = asyncio.create_task(self._warm_maintainer())
        logger.info(f"ProcessBroker started, warm_pool={self.warm_pool_size}")

    async def stop(self):
        # If drain is in progress, wait for it to complete
        if self._draining and self._drain_event and not self._drain_event.is_set():
            try:
                await asyncio.wait_for(self._drain_event.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
        for t in (self._cleanup_task, self._warm_task):
            if t:
                t.cancel()
        for sid in list(self._warm):
            await self._destroy_warm(sid)
        for uid in list(self._procs):
            await self.release(uid)
        logger.info("ProcessBroker stopped")

    async def _cleanup_stale_processes(self):
        """Kill Hermes dashboard processes that are listening on broker ports
        but not managed by this broker instance (leftover from crash/restart)."""
        try:
            result = await asyncio.create_subprocess_exec(
                "ss", "-tlnp",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await result.communicate()
            for line in stdout.decode().splitlines():
                # Only consider ports in broker's range
                m_port = re.search(r':(\d+)', line)
                if not m_port:
                    continue
                port = int(m_port.group(1))
                if port < self.base_port or port > self.max_port:
                    continue
                m = re.search(r'pid=(\d+)', line)
                if m:
                    pid = int(m.group(1))
                    try:
                        os.kill(pid, 9)
                        logger.info(f"清理残留 Hermes 进程 PID={pid} port={port}")
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        pass
        except Exception as e:
            logger.warning(f"清理残留进程失败: {e}")

    # ── 核心接口：分配 ────────────────────────────────────

    async def acquire(self, user_id: str) -> dict:
        """
        为用户分配一个 Hermes 进程。
        如果已有活跃进程直接返回；否则从预热池取或冷启动。

        Concurrency: a per-user asyncio.Lock serialises the "check existing
        → spawn" window so two simultaneous requests for the same user_id
        cannot each spawn a fresh dashboard. The broker-wide lock is still
        used to safely fetch/create the per-user lock and to mutate shared
        state (_procs, _free_ports).
        """
        # Drain mode — reject new allocations
        if self._draining:
            raise RuntimeError("Broker is draining, not accepting new connections")

        # Fast-path: existing live proc → no per-user lock needed (read-only).
        async with self._lock:
            existing = self._procs.get(user_id)
            if existing and existing.status == "active":
                # Detect crashed/killed processes
                try:
                    os.kill(existing.pid, 0)
                except ProcessLookupError:
                    logger.info(f"[{user_id}] 进程 PID={existing.pid} 已死亡，重新分配")
                    self._procs.pop(user_id, None)
                    self._free_port(existing.port)
                    existing = None
                except OSError:
                    pass
                if existing:
                    existing.touch()
                    return self._make_info(existing)

            # Get-or-create the per-user lock under the broker lock so the
            # mapping itself stays consistent. The lock object outlives this
            # acquire() call so concurrent callers serialise on the same one.
            user_lock = self._user_locks.get(user_id)
            if user_lock is None:
                user_lock = asyncio.Lock()
                self._user_locks[user_id] = user_lock

        # Serialise the spawn section for this user_id.
        async with user_lock:
            # Re-check inside the user lock: another coroutine for the same
            # user_id may have completed the spawn while we were waiting.
            async with self._lock:
                existing = self._procs.get(user_id)
                if existing and existing.status == "active":
                    try:
                        os.kill(existing.pid, 0)
                    except ProcessLookupError:
                        self._procs.pop(user_id, None)
                        self._free_port(existing.port)
                        existing = None
                    except OSError:
                        pass
                    if existing:
                        existing.touch()
                        return self._make_info(existing)

                # Capacity check (post-recheck, so a freed slot from a
                # crashed proc above is honoured here).
                if len(self._procs) >= self.max_users:
                    raise RuntimeError(f"已达最大用户数 {self.max_users}")

            # Release broker lock around the slow spawn path so other users
            # aren't blocked. Per-user lock still held → only one spawn per
            # user_id at a time.
            proc = await self._take_warm(user_id)
            if proc is None:
                proc = await self._spawn(user_id)

            proc.status = "active"
            async with self._lock:
                self._procs[user_id] = proc

            return self._make_info(proc)

    async def release(self, user_id: str, flush: bool = True):
        async with self._lock:
            proc = self._procs.pop(user_id, None)
            # Drop the per-user lock so the dict doesn't grow unbounded
            # across long broker uptimes. Safe to drop here because anyone
            # who later calls acquire() for this user will get a fresh lock.
            self._user_locks.pop(user_id, None)
        if not proc:
            return
        if flush:
            await self._flush_and_kill(proc)
        else:
            await self._kill(proc)
        self._free_port(proc.port)
        # Only clean up logs, preserve user-generated files in work_dir
        if proc.work_dir and os.path.exists(proc.work_dir):
            logs_dir = Path(proc.work_dir) / "logs"
            if logs_dir.is_dir():
                shutil.rmtree(logs_dir, ignore_errors=True)

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

        user_base = self.work_root / (user_id if user_id != "__warm__" else "__warm__")

        if session_id is None:
            # Reuse existing orphan work_dir if any (survives broker restart)
            if user_base.is_dir():
                existing_dirs = sorted(
                    (d for d in user_base.iterdir() if d.is_dir() and d.name not in ("hermes_home",)),
                    key=lambda d: d.stat().st_mtime,
                    reverse=True,
                )
                session_id = existing_dirs[0].name if existing_dirs else str(uuid.uuid4())
            else:
                session_id = str(uuid.uuid4())

        work_dir = user_base / session_id
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

        # Skills: symlink system skills from global HERMES_HOME
        for dname in ("skills",):
            src = _HERMES_CONFIG_HOME / dname
            dst = user_home / dname
            if src.is_dir() and not (dst.exists() or dst.is_symlink()):
                dst.mkdir(parents=True, exist_ok=True)
                for skill_entry in src.iterdir():
                    if skill_entry.is_dir():
                        os.symlink(str(skill_entry), str(dst / skill_entry.name))

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
            proc_env["HERMES_WRITE_SAFE_ROOT"] = str(work_dir)
            proc_env["HERMES_NGINX_DOMAIN"] = os.environ.get("HERMES_NGINX_DOMAIN", "")
            # Inject OPENAI_BASE_URL + OPENAI_API_KEY so the auxiliary client
            # (vision, title gen, etc.) can resolve the "custom" provider.
            # Without these, bare "custom" falls back to credential pool which
            # may pick an unrelated provider (e.g. minimax-cn).
            _cfg_base, _cfg_key = _read_config_base_url(user_home)
            if _cfg_base:
                proc_env["OPENAI_BASE_URL"] = _cfg_base
            if _cfg_key:
                proc_env["OPENAI_API_KEY"] = _cfg_key

            logger.info(f"[{user_id}] 启动 dashboard port={port}")
            proc.process = await asyncio.create_subprocess_exec(
                _HERMES_PYTHON, "-m", _HERMES_MODULE,
                "dashboard", "--skip-build",
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
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as sess:
                for i in range(30):
                    await asyncio.sleep(0.3 if i < 5 else 0.5)
                    try:
                        async with sess.get(url + "/") as resp:
                            if resp.status != 200:
                                continue
                            html = await resp.text()
                            m = TOKEN_RE.search(html)
                            if m:
                                proc.ws_token = m.group(1)
                                return
                    except Exception:
                        continue
        finally:
            pass
        raise RuntimeError("dashboard 启动超时")

    async def _flush_and_kill(self, proc: HermesProcess):
        """Notify Hermes process to flush state, then terminate gracefully."""
        p = proc.process
        if p is None or p.returncode is not None:
            return
        # Ask Hermes to close all sessions (triggers state.db flush)
        try:
            async with aiohttp.ClientSession() as session:
                url = f"http://127.0.0.1:{proc.port}/api/sessions"
                headers = {"Authorization": f"Bearer {proc.ws_token}"}
                async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status == 200:
                        sessions = await resp.json()
                        for s in sessions:
                            key = s.get("sessionKey") or s.get("key")
                            if key:
                                try:
                                    await session.delete(
                                        f"http://127.0.0.1:{proc.port}/api/sessions/{key}",
                                        headers=headers,
                                        timeout=aiohttp.ClientTimeout(total=3),
                                    )
                                except Exception:
                                    pass
        except Exception:
            pass
        # Now terminate — Hermes SIGTERM handler will drain remaining work
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), timeout=10)
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

    async def _kill(self, proc: HermesProcess):
        """Quick kill without flush (for warm pool cleanup, etc.)."""
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

    # ── Graceful drain ──────────────────────────────────────

    async def start_drain(self, timeout: int = 300) -> dict:
        """Begin graceful drain. Returns immediately with initial status."""
        async with self._lock:
            if self._draining:
                return {"status": "already_draining", "started_at": self._drain_started_at, **self._drain_status()}

            self._draining = True
            self._drain_event = asyncio.Event()
            self._drain_started_at = time.time()

            # Snapshot current processes and warm pool
            user_ids = list(self._procs.keys())
            warm_ids = list(self._warm.keys())

            for uid in user_ids:
                self._drain_progress[uid] = "pending"
            for sid in warm_ids:
                self._drain_progress[sid] = "pending_warm"

        # Cancel warm pool maintainer
        if self._warm_task:
            self._warm_task.cancel()
            self._warm_task = None

        # Kill warm pool (no sessions to flush)
        for sid in warm_ids:
            self._drain_progress[sid] = "flushing"
            await self._destroy_warm(sid)
            self._drain_progress[sid] = "done"

        # Stop cleanup loop to prevent race
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None

        if not user_ids:
            self._drain_event.set()
            logger.info("[drain] No active processes to drain")
        else:
            self._drain_task = asyncio.create_task(self._drain_users(user_ids, timeout))
            logger.info(f"[drain] Starting drain for {len(user_ids)} processes, timeout={timeout}s")

        return {"status": "draining", "started_at": self._drain_started_at, **self._drain_status()}

    async def _drain_users(self, user_ids: list, timeout: int):
        """Background task: flush and kill all user processes concurrently."""
        semaphore = asyncio.Semaphore(10)

        async def _drain_one(uid: str):
            async with semaphore:
                self._drain_progress[uid] = "flushing"
                try:
                    async with self._lock:
                        proc = self._procs.pop(uid, None)
                    if proc:
                        await self._flush_and_kill(proc)
                        self._free_port(proc.port)
                        if proc.work_dir and os.path.exists(proc.work_dir):
                            logs_dir = Path(proc.work_dir) / "logs"
                            if logs_dir.is_dir():
                                shutil.rmtree(logs_dir, ignore_errors=True)
                    self._drain_progress[uid] = "done"
                except Exception as e:
                    logger.error(f"[drain] Failed to drain {uid}: {e}")
                    self._drain_progress[uid] = f"error: {e}"

        try:
            await asyncio.wait_for(
                asyncio.gather(*[_drain_one(uid) for uid in user_ids], return_exceptions=True),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning(f"[drain] Timed out after {timeout}s")
            for uid, status in list(self._drain_progress.items()):
                if status not in ("done",) and not status.startswith("error") and not status.startswith("done_warm"):
                    self._drain_progress[uid] = "timeout"
        finally:
            self._drain_event.set()
            logger.info("[drain] Drain complete")

    async def cancel_drain(self) -> dict:
        """Cancel an in-progress drain."""
        async with self._lock:
            if not self._draining:
                return {"status": "not_draining"}

            if self._drain_task and not self._drain_task.done():
                self._drain_task.cancel()
                try:
                    await self._drain_task
                except asyncio.CancelledError:
                    pass

            self._draining = False
            self._drain_event = None
            self._drain_task = None
            self._drain_progress.clear()

            # Restart background loops
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())
            self._warm_task = asyncio.create_task(self._warm_maintainer())

        logger.info("[drain] Drain cancelled")
        return {"status": "cancelled"}

    def _drain_status(self) -> dict:
        """Return drain progress info."""
        if not self._draining:
            return {"draining": False}
        total = len(self._drain_progress)
        done = sum(1 for s in self._drain_progress.values() if s in ("done", "done_warm"))
        errors = sum(1 for s in self._drain_progress.values() if s.startswith("error") or s == "timeout")
        pending = total - done - errors
        elapsed = time.time() - (self._drain_started_at or time.time())
        return {
            "draining": True,
            "total": total,
            "done": done,
            "pending": pending,
            "errors": errors,
            "elapsed_seconds": round(elapsed, 1),
            "processes": dict(self._drain_progress),
        }

    # ── 后台清理 ──────────────────────────────────────────

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                to_close = []
                for uid, p in list(self._procs.items()):
                    if p.status != "active":
                        continue
                    # Detect crashed/killed processes
                    try:
                        os.kill(p.pid, 0)
                    except ProcessLookupError:
                        logger.info(f"[{uid}] 进程 PID={p.pid} 已死亡，回收")
                        to_close.append(uid)
                        continue
                    except OSError:
                        pass
                    if now - p.last_active_at > self.idle_timeout:
                        # WS goes direct to dashboard (nginx), not via broker,
                        # so last_active_at misses WS activity. Check if the
                        # dashboard still has active WS clients before killing.
                        if await self._has_ws_clients(p.port):
                            p.touch()
                            logger.debug(f"[{uid}] WS still active, touched")
                            continue
                        to_close.append(uid)
                for uid in to_close:
                    logger.info(f"[{uid}] 空闲超时 {self.idle_timeout}s，回收")
                    await self.release(uid)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理异常: {e}")

    async def _has_ws_clients(self, port: int) -> bool:
        """Check if the dashboard port has established TCP connections
        (proxy for active WS clients). WS goes direct via nginx, not broker."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "ss", "-tn", f"( sport = :{port} )",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            # ss output: header line + one line per connection
            lines = stdout.decode().strip().split("\n")
            return len(lines) > 1
        except Exception:
            return False

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

# Persist JWT signing secret across restarts so existing cookies stay valid.
# If the secret rotated on every restart, every user would be force-logged-out
# after a broker code reload. Stored alongside .env with mode 0600.
# To force-rotate (e.g. after suspected leak), `rm` this file before restart.
_JWT_SECRET_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwt_secret")


def _load_or_create_jwt_secret() -> str:
    """Read JWT secret from disk; generate + persist if absent.

    Atomic write via temp file + rename. File mode 0600 so only the broker
    process owner can read it.
    """
    try:
        with open(_JWT_SECRET_FILE, "r") as f:
            secret = f.read().strip()
        if len(secret) >= 32:
            return secret
    except FileNotFoundError:
        pass
    except OSError:
        pass
    # Generate new
    secret = secrets.token_hex(32)
    try:
        tmp = _JWT_SECRET_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(secret)
        os.chmod(tmp, 0o600)
        os.rename(tmp, _JWT_SECRET_FILE)
    except OSError:
        # Fall back to in-memory secret if we can't persist — broker still
        # works, but next restart will rotate the secret.
        pass
    return secret


_JWT_SECRET = _load_or_create_jwt_secret()
_JWT_ALG = "HS256"
_JWT_EXP_SECONDS = 7 * 24 * 3600  # 7 days
_OAUTH_STATE_STORE: Dict[str, float] = {}  # state → created_at, in-memory


def _read_config_base_url(user_home) -> tuple:
    """Read the custom provider's (base_url, api_key) from the user's config.yaml.

    Used to inject OPENAI_BASE_URL + OPENAI_API_KEY into the process
    environment so the auxiliary client (vision, title gen, etc.) can
    resolve the bare ``custom`` provider to the correct endpoint.
    """
    try:
        import yaml
        cfg_path = Path(user_home) / "config.yaml"
        if not cfg_path.is_file():
            return ("", "")
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        # Check custom_providers list first
        for entry in (cfg.get("custom_providers") or []):
            if isinstance(entry, dict) and entry.get("base_url"):
                return (
                    str(entry["base_url"]).strip().rstrip("/"),
                    str(entry.get("api_key") or "").strip(),
                )
        # Fallback: model.base_url
        model = cfg.get("model") or {}
        if isinstance(model, dict):
            bu = str(model.get("base_url") or "").strip()
            if bu:
                return (bu.rstrip("/"), "")
        return ("", "")
    except Exception:
        return ("", "")


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


app = FastAPI(title="Hermes Process Broker", lifespan=lifespan, docs_url=None, redoc_url=None)


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


@app.get("/broker/files/{user_id}/{filename}")
async def download_file(user_id: str, filename: str):
    """Serve an uploaded file for download."""
    proc = broker.get(user_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No active session")
    fpath = Path(proc.work_dir) / "uploads" / filename
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fpath), filename=filename)


@app.get("/broker/health")
async def health():
    status = "draining" if broker._draining else "ok"
    result = {"status": status, **broker.stats()}
    if broker._draining:
        result["drain"] = broker._drain_status()
    return result


@app.post("/broker/reload")
async def reload_config():
    """Graceful reload: re-read broker code without killing child Hermes processes."""
    import importlib
    logger.info("Broker reload requested")
    return {"status": "ok", "note": "Child processes preserved"}


@app.post("/broker/reload-mcp/{user_id}")
async def reload_mcp_for_user(user_id: str):
    """Forward reload.mcp RPC to a specific user's Hermes dashboard process."""
    proc = broker._procs.get(user_id)
    if proc is None:
        raise HTTPException(status_code=404, detail=f"No process for user {user_id}")
    try:
        import json
        ws_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
        session = await _get_proxy_http()
        async with session.ws_connect(ws_url) as hermes_ws:
            reload_req = json.dumps({"jsonrpc": "2.0", "id": "_reload_mcp", "method": "reload.mcp", "params": {}})
            await hermes_ws.send_str(reload_req)
            async for msg in hermes_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("id") == "_reload_mcp":
                            logger.info(f"[{user_id}] reload.mcp forwarded OK → port {proc.port}")
                            return {"status": "ok", "port": proc.port, "response": data.get("result")}
                    except Exception:
                        break
        return {"status": "ok", "port": proc.port}
    except Exception as e:
        logger.warning(f"[{user_id}] reload.mcp failed: {e}")
        raise HTTPException(status_code=502, detail=str(e))


# ── Graceful drain ──────────────────────────────────────

@app.post("/broker/drain")
async def drain_start(request: Request):
    """Start graceful drain: flush all Hermes processes, block new connections."""
    body = {}
    try:
        raw = await request.body()
        if raw:
            body = json.loads(raw)
    except Exception:
        pass

    timeout = body.get("timeout", 300)
    wait = body.get("wait", False)

    result = await broker.start_drain(timeout=timeout)

    if wait and broker._drain_event:
        remaining = timeout - (time.time() - (broker._drain_started_at or time.time()))
        if remaining > 0:
            try:
                await asyncio.wait_for(broker._drain_event.wait(), timeout=remaining)
            except asyncio.TimeoutError:
                pass
        result = {
            "status": "drain_complete" if all(
                v in ("done", "done_warm") for v in broker._drain_progress.values()
            ) else "drain_timed_out",
            **broker._drain_status(),
        }

    return result


@app.get("/broker/drain")
async def drain_status():
    """Poll drain progress."""
    return broker._drain_status()


@app.post("/broker/drain/cancel")
async def drain_cancel():
    """Cancel an in-progress drain."""
    return await broker.cancel_drain()


@app.get("/broker/stats")
async def stats():
    return broker.stats()


# ─────────────────────────────────────────────────────────
# Proxy API — 对外唯一入口，隐藏内部端口/token
# ─────────────────────────────────────────────────────────

# aiohttp session for backend HTTP calls (reused)
_proxy_http: Optional[aiohttp.ClientSession] = None


async def _get_proxy_http() -> aiohttp.ClientSession:
    global _proxy_http
    if _proxy_http is None or _proxy_http.closed:
        _proxy_http = aiohttp.ClientSession()
    return _proxy_http


async def _proc_for_request(request: Request) -> HermesProcess:
    """Authenticate and return the user's process. Supports JWT cookie,
    X-Hermes-Session-Token (dashboard session token), and legacy user_id."""
    # 1. JWT cookie
    user = _get_session_user(request)
    if user:
        user_id = user["sub"]
    else:
        user_id = ""

    # 2. X-Hermes-Session-Token — find the proc that owns this token
    if not user_id:
        session_token = request.headers.get("X-Hermes-Session-Token", "")
        if session_token:
            for uid, proc in broker._procs.items():
                if getattr(proc, 'ws_token', '') == session_token:
                    user_id = uid
                    break

    # 3. Legacy fallback: header or body
    if not user_id:
        user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        try:
            body = await request.json()
            user_id = (body or {}).get("user_id", "")
        except Exception:
            pass

    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    proc = broker.get(user_id)
    if not proc:
        raise HTTPException(status_code=404, detail="No active session")
    proc.touch()
    return proc


async def _hermes_http(proc: HermesProcess, method: str, path: str,
                       body: bytes = b"", content_type: str = "") -> Response:
    """Forward an HTTP request to the user's Hermes process."""
    url = f"http://127.0.0.1:{proc.port}{path}"
    headers = {"Authorization": f"Bearer {proc.ws_token}"}
    if content_type:
        headers["Content-Type"] = content_type
    session = await _get_proxy_http()
    async with session.request(method, url, headers=headers, data=body) as resp:
        resp_body = await resp.read()
        return Response(
            content=resp_body,
            status_code=resp.status,
            media_type=resp.headers.get("Content-Type", "application/json"),
        )


# ── Session allocation (no sensitive info exposed) ────────

@app.post("/api/sessions")
async def proxy_acquire(request: Request):
    """Allocate a Hermes process. Returns only safe fields."""
    # Auth: JWT cookie preferred, fallback to body.user_id
    user = _get_session_user(request)
    if user:
        user_id = user["sub"]
    else:
        try:
            body = await request.json()
            user_id = (body or {}).get("user_id", "")
        except Exception:
            user_id = ""
        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        info = await broker.acquire(user_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    proc = broker.get(user_id)
    if proc:
        proc.touch()
    return {
        "user_id": info["user_id"],
        "status": info["status"],
        "created_at": info["created_at"],
    }


@app.delete("/api/sessions")
async def proxy_release(request: Request):
    """Release the user's Hermes process."""
    user = _get_session_user(request)
    if user:
        user_id = user["sub"]
    else:
        user_id = request.headers.get("X-User-ID", "")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    await broker.release(user_id)
    return {"status": "released"}


# ── WebSocket proxy ──────────────────────────────────────

@app.websocket("/api/ws")
async def ws_proxy(websocket: WebSocket):
    """Bidirectional WS proxy: browser ←→ Hermes process."""
    # Auth: read cookie from handshake headers
    cookie_header = websocket.headers.get("cookie", "")
    token = None
    for part in cookie_header.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "hermes_session":
            token = v
            break
    if not token:
        await websocket.close(code=4001, reason="Not authenticated")
        return
    user = _verify_jwt(token)
    if not user:
        await websocket.close(code=4001, reason="Invalid session")
        return
    proc = broker.get(user["sub"])
    if not proc:
        await websocket.close(code=4004, reason="No active session")
        return
    if broker._draining:
        await websocket.close(code=4003, reason="Broker is draining")
        return
    proc.touch()

    await websocket.accept()

    hermes_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
    session = await _get_proxy_http()
    try:
        async with session.ws_connect(hermes_url, max_msg_size=0) as hermes_ws:

            async def forward_to_hermes():
                try:
                    while True:
                        data = await websocket.receive_text()
                        proc.touch()
                        await hermes_ws.send_str(data)
                except (WebSocketDisconnect, Exception):
                    pass
                finally:
                    try:
                        await hermes_ws.close()
                    except Exception:
                        pass

            async def forward_to_browser():
                try:
                    async for msg in hermes_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            proc.touch()
                            await websocket.send_text(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                except Exception:
                    pass
                finally:
                    try:
                        await websocket.close()
                    except Exception:
                        pass

            await asyncio.gather(
                forward_to_hermes(),
                forward_to_browser(),
                return_exceptions=True,
            )
    except Exception as e:
        logger.warning(f"WS proxy error for {user['sub']}: {e}")
        try:
            await websocket.close(code=1011, reason="Proxy error")
        except Exception:
            pass



# ── File upload/download via proxy ───────────────────────

@app.post("/api/upload")
async def proxy_upload(request: Request):
    """Upload file to user's process. Supports multipart/form-data and JSON (base64)."""
    import base64
    proc = await _proc_for_request(request)
    ct = request.headers.get("content-type", "")

    if ct.startswith("multipart/form-data"):
        # Original multipart path
        form = await request.form()
        upload = form.get("file")
        if not upload or not upload.filename:
            raise HTTPException(status_code=400, detail="file field required")
        filename = upload.filename
        content = await upload.read()
    elif ct.startswith("application/json"):
        # JSON + base64 path (for proxies that can't forward multipart)
        body = await request.json()
        filename = body.get("filename") or body.get("name")
        b64 = body.get("data") or body.get("content") or body.get("file")
        if not filename or not b64:
            raise HTTPException(status_code=400, detail="filename and data (base64) required")
        content = base64.b64decode(b64)
    else:
        raise HTTPException(status_code=415, detail="Content-Type must be multipart/form-data or application/json")

    uploads_dir = Path(proc.work_dir) / "uploads"
    uploads_dir.mkdir(parents=True, exist_ok=True)

    safe_name = re.sub(r'[^\w.\-]', '_', filename)
    dest = uploads_dir / safe_name
    dest.write_bytes(content)

    logger.info(f"[{proc.user_id}] File uploaded: {dest} ({len(content)} bytes)")
    return {"path": str(dest), "name": safe_name, "size": len(content)}


@app.get("/api/files")
async def list_files(request: Request, scope: str = "all", session: str = ""):
    """List files (JWT auth).
    session=current: only current session's work_dir.
    session=<dir_name>: only that session directory.
    session="" (default, scope=all): all session work_dirs under user_root."""
    proc = await _proc_for_request(request)
    result = []
    _skip = {"hermes_home", "logs", "__pycache__"}

    def _scan(directory: Path, prefix: str = ""):
        try:
            entries = sorted(directory.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True)
        except PermissionError:
            return
        for entry in entries:
            if entry.name.startswith('.') or entry.name in _skip:
                continue
            if entry.is_file():
                stat = entry.stat()
                result.append({
                    "path": prefix + entry.name if prefix else entry.name,
                    "name": entry.name,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                })
            elif entry.is_dir():
                _scan(entry, prefix + entry.name + "/")

    if session == "current" or (not session and scope == "current"):
        work_dir = Path(proc.work_dir)
        if work_dir.is_dir():
            _scan(work_dir, work_dir.name + "/")
    elif session:
        # Specific session directory
        user_root = Path(broker.work_root) / proc.user_id
        target = user_root / session
        if target.is_dir():
            _scan(target, session + "/")
    else:
        # All sessions
        user_root = Path(broker.work_root) / proc.user_id
        if user_root.is_dir():
            for entry in sorted(user_root.iterdir()):
                if entry.name.startswith('.') or entry.name in _skip:
                    continue
                if entry.is_dir():
                    _scan(entry, entry.name + "/")

    logger.info(f"[{proc.user_id}] list_files: found {len(result)} files")
    return result


@app.get("/api/workdirs")
async def list_work_dirs(request: Request):
    """List all work directories for the current user (JWT auth)."""
    proc = await _proc_for_request(request)
    user_root = Path(broker.work_root) / proc.user_id
    _skip = {"hermes_home", "logs", "__pycache__", "uploads"}
    dirs = []
    if user_root.is_dir():
        for entry in sorted(user_root.iterdir(), key=lambda d: d.stat().st_mtime, reverse=True):
            if not entry.is_dir() or entry.name.startswith('.') or entry.name in _skip:
                continue
            try:
                file_count = sum(1 for f in entry.iterdir()
                                 if f.is_file() and not f.name.startswith('.'))
                dir_count = sum(1 for f in entry.iterdir()
                                if f.is_dir() and not f.name.startswith('.') and f.name not in _skip)
            except PermissionError:
                file_count = dir_count = 0
            stat = entry.stat()
            dirs.append({
                "name": entry.name,
                "file_count": file_count,
                "dir_count": dir_count,
                "mtime": stat.st_mtime,
                "is_current": entry.name == Path(proc.work_dir).name,
            })
    return dirs


async def proxy_download(file_path: str, request: Request):
    """Serve a file from current session's work_dir for download (JWT auth)."""
    proc = await _proc_for_request(request)
    user_root = Path(broker.work_root) / proc.user_id
    fpath = user_root / file_path
    # Path traversal protection
    try:
        fpath = fpath.resolve()
        if not str(fpath).startswith(str(user_root.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid path")
    # Block sensitive directories, but allow screenshots cache
    _blocked = {"hermes_home", "logs"}
    rel = fpath.relative_to(user_root.resolve())
    parts = list(rel.parts)
    if parts[:3] != ["hermes_home", "cache", "screenshots"]:
        for part in parts:
            if part.startswith('.') or part in _blocked:
                raise HTTPException(status_code=403, detail="Access denied")
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(str(fpath), filename=fpath.name)


@app.get("/api/files/download/{file_path:path}")
async def download_files(file_path: str, request: Request):
    """Download a file or folder as zip archive (JWT auth)."""
    import zipfile, io

    proc = await _proc_for_request(request)
    user_root = Path(broker.work_root) / proc.user_id
    fpath = user_root / file_path
    # Path traversal protection
    try:
        fpath = fpath.resolve()
        if not str(fpath).startswith(str(user_root.resolve())):
            raise HTTPException(status_code=403, detail="Access denied")
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid path")
    _blocked = {"hermes_home", "logs"}
    rel = fpath.relative_to(user_root.resolve())
    parts = list(rel.parts)
    if parts[:3] != ["hermes_home", "cache", "screenshots"]:
        for part in parts:
            if part.startswith('.') or part in _blocked:
                raise HTTPException(status_code=403, detail="Access denied")

    if fpath.is_file():
        # Serve image files inline so browsers display them instead of downloading
        _img_ext = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp', '.svg'}
        if fpath.suffix.lower() in _img_ext:
            return FileResponse(str(fpath))
        return FileResponse(str(fpath), filename=fpath.name)

    if fpath.is_dir():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(fpath):
                for fname in sorted(files):
                    f = Path(root) / fname
                    if f.is_file() and not fname.startswith('.'):
                        arcname = Path(f).relative_to(fpath)
                        zf.write(str(f), str(arcname))
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={fpath.name}.zip"},
        )

    raise HTTPException(status_code=404, detail="Not found")


@app.get("/api/screenshot")
async def serve_screenshot(request: Request):
    """Serve a MEDIA: file by absolute path (JWT auth).

    The agent emits MEDIA:/absolute/path in responses. The frontend converts
    these to /api/screenshot?path=<encoded>. This endpoint validates the path
    is under the user's Hermes directory (cache, uploads, or work files).
    """
    from urllib.parse import unquote
    abs_path = unquote(request.query_params.get("path", ""))
    if not abs_path:
        raise HTTPException(status_code=400, detail="Missing path")
    proc = await _proc_for_request(request)
    user_root = Path(broker.work_root) / proc.user_id

    # Relative path → search in user directories (work_dir, cache, uploads)
    if not Path(abs_path).is_absolute():
        for candidate in [
            Path(proc.work_dir) / abs_path,
            user_root / "hermes_home" / "cache" / "screenshots" / abs_path,
            user_root / "uploads" / abs_path,
        ]:
            if candidate.is_file():
                abs_path = str(candidate)
                break
        else:
            raise HTTPException(status_code=404, detail="Not found")

    allowed_dirs = [
        (user_root / "hermes_home" / "cache").resolve(),
        (user_root / "uploads").resolve(),
        # Also allow the work_dir itself for relative paths resolved above
        Path(proc.work_dir).resolve(),
    ]
    try:
        fpath = Path(abs_path).resolve()
        if not any(str(fpath).startswith(str(d)) for d in allowed_dirs):
            raise HTTPException(status_code=403, detail="Access denied")
    except (ValueError, OSError):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not fpath.is_file():
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(str(fpath))


@app.post("/api/skills/upload")
async def proxy_skill_upload(request: Request):
    """Upload a skill zip and install to user's Hermes skills directory."""
    import base64
    proc = await _proc_for_request(request)
    ct = request.headers.get("content-type", "")

    if ct.startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        if not upload or not upload.filename:
            raise HTTPException(status_code=400, detail="file field required")
        filename = upload.filename
        content = await upload.read()
    elif ct.startswith("application/json"):
        body = await request.json()
        filename = body.get("filename") or body.get("name", "")
        b64 = body.get("data") or body.get("content") or body.get("file")
        if not filename or not b64:
            raise HTTPException(status_code=400, detail="filename and data (base64) required")
        content = base64.b64decode(b64)
    else:
        raise HTTPException(status_code=415, detail="Content-Type must be multipart/form-data or application/json")

    if not filename.endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are supported")

    # Determine skills directory: {hermes_home}/skills/
    user_home = Path(proc.work_dir).parent / "hermes_home"
    skills_dir = user_home / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    # Extract zip
    import tempfile
    skill_name = ""
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "upload.zip"
        zip_path.write_bytes(content)
        try:
            with zipfile.ZipFile(str(zip_path), "r") as zf:
                zf.extractall(tmp)
        except zipfile.BadZipFile:
            raise HTTPException(status_code=400, detail="Invalid zip file")

        # Find skill directory: either root of zip or single top-level dir
        # Filter out the zip file itself, hidden dirs (__MACOSX), and macOS metadata
        _skip = {"upload.zip", "__MACOSX", ".DS_Store"}
        entries = [p for p in Path(tmp).iterdir() if p.name not in _skip and not p.name.startswith(".")]
        logger.info(f"[{proc.user_id}] Zip entries: {[e.name for e in entries]}, all: {[p.name for p in Path(tmp).iterdir()]}")
        if len(entries) == 1 and entries[0].is_dir():
            src_dir = entries[0]
        elif len(entries) == 0:
            raise HTTPException(status_code=400, detail="Empty zip file")
        else:
            # Check if SKILL.md is at root level
            if (Path(tmp) / "SKILL.md").exists():
                src_dir = Path(tmp)
            else:
                # Try finding a subdirectory with SKILL.md
                found = None
                for e in entries:
                    if e.is_dir() and (e / "SKILL.md").exists():
                        found = e
                        break
                if not found:
                    raise HTTPException(status_code=400, detail="Invalid skill: missing SKILL.md")
                src_dir = found

        # Validate: must have SKILL.md
        if not (src_dir / "SKILL.md").exists():
            raise HTTPException(status_code=400, detail="Invalid skill: missing SKILL.md")

        skill_name = src_dir.name
        dest = skills_dir / skill_name
        if dest.is_symlink():
            dest.unlink()
        elif dest.exists():
            shutil.rmtree(str(dest))
        shutil.copytree(str(src_dir), str(dest))

    logger.info(f"[{proc.user_id}] Skill installed: {skill_name}")

    # Trigger skills.reload via WS
    try:
        import json
        ws_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
        session = await _get_proxy_http()
        async with session.ws_connect(ws_url) as hermes_ws:
            reload_req = json.dumps({"jsonrpc": "2.0", "id": "_skill_reload", "method": "skills.reload", "params": {}})
            await hermes_ws.send_str(reload_req)
            async for msg in hermes_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("id") == "_skill_reload":
                            break
                    except Exception:
                        break
    except Exception as e:
        logger.warning(f"[{proc.user_id}] skills.reload failed after upload: {e}")

    return {"status": "installed", "name": skill_name}


@app.get("/api/skills/list")
async def proxy_skills_list(request: Request):
    """List skills with system/user classification. Syncs new system skills automatically."""
    proc = await _proc_for_request(request)
    user_home = Path(proc.work_dir).parent / "hermes_home"
    user_skills_dir = user_home / "skills"
    global_skills_dir = _HERMES_CONFIG_HOME / "skills"

    # Sync: ensure system skills are symlinks to global HERMES_HOME
    needs_reload = False
    if global_skills_dir.is_dir():
        for skill_entry in global_skills_dir.iterdir():
            if not skill_entry.is_dir():
                continue
            user_skill = user_skills_dir / skill_entry.name
            if not user_skill.exists():
                os.symlink(str(skill_entry), str(user_skill))
                logger.info(f"[{proc.user_id}] Synced system skill: {skill_entry.name}")
                needs_reload = True
            elif user_skill.is_dir() and not user_skill.is_symlink():
                shutil.rmtree(str(user_skill))
                os.symlink(str(skill_entry), str(user_skill))
                logger.info(f"[{proc.user_id}] Migrated system skill to symlink: {skill_entry.name}")
                needs_reload = True

    if needs_reload:
        try:
            ws_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
            session = await _get_proxy_http()
            async with session.ws_connect(ws_url) as ws:
                await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": "_sync_reload", "method": "skills.reload", "params": {}}))
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            data = json.loads(msg.data)
                            if data.get("id") == "_sync_reload":
                                break
                        except Exception:
                            break
        except Exception as e:
            logger.warning(f"[{proc.user_id}] skills.reload after sync failed: {e}")

    # Get skills from Hermes process
    session = await _get_proxy_http()
    url = f"http://127.0.0.1:{proc.port}/api/skills"
    headers = {"Authorization": f"Bearer {proc.ws_token}"}
    async with session.get(url, headers=headers) as resp:
        skills_data = await resp.json()
    skills_list = skills_data if isinstance(skills_data, list) else skills_data.get("skills", [])

    # Mark each skill as system or user
    for s in skills_list:
        skill_path = user_skills_dir / s["name"]
        s["user_installed"] = skill_path.is_dir() and not skill_path.is_symlink()

    return skills_list


@app.delete("/api/skills/{skill_name}")
async def proxy_skill_delete(skill_name: str, request: Request):
    """Delete a user-installed skill."""
    proc = await _proc_for_request(request)
    user_home = Path(proc.work_dir).parent / "hermes_home"
    skill_path = user_home / "skills" / skill_name

    if not skill_path.exists():
        raise HTTPException(status_code=404, detail="Skill not found")
    if skill_path.is_symlink():
        raise HTTPException(status_code=400, detail="Cannot delete system skill")

    shutil.rmtree(str(skill_path))
    logger.info(f"[{proc.user_id}] Skill deleted: {skill_name}")

    # Trigger skills.reload via WS
    try:
        import json
        ws_url = f"ws://127.0.0.1:{proc.port}/api/ws?token={proc.ws_token}"
        session = await _get_proxy_http()
        async with session.ws_connect(ws_url) as hermes_ws:
            await hermes_ws.send_str(json.dumps({"jsonrpc": "2.0", "id": "_skill_del", "method": "skills.reload", "params": {}}))
            async for msg in hermes_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                        if data.get("id") == "_skill_del":
                            break
                    except Exception:
                        break
    except Exception as e:
        logger.warning(f"[{proc.user_id}] skills.reload after delete failed: {e}")

    return {"status": "deleted", "name": skill_name}


# ── API Documentation ──────────────────────────────────────────────

@app.get("/docs/{name}")
async def serve_api_doc(name: str):
    """Render a markdown doc from docs/ as HTML."""
    import pathlib
    import markdown
    _allowed = {"api", "api-mcp-oauth", "api-skills"}
    if name not in _allowed:
        raise HTTPException(status_code=404, detail="Document not found")
    doc_path = pathlib.Path(__file__).parent / "docs" / f"{name}.md"
    if not doc_path.is_file():
        raise HTTPException(status_code=404, detail="Document not found")
    md_text = doc_path.read_text(encoding="utf-8")
    body = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "toc", "codehilite"],
        extension_configs={"codehilite": {"css_class": "highlight", "guess_lang": False}},
    )
    # Collect API doc names for sidebar (only api* prefixed files)
    docs_dir = pathlib.Path(__file__).parent / "docs"
    nav_items = []
    for f in sorted(docs_dir.glob("api*.md")):
        slug = f.stem
        title = slug.replace("-", " ").replace("_", " ").title()
        active = " active" if slug == name else ""
        nav_items.append(f'<li><a href="/docs/{slug}" class="nav-link{active}">{title}</a></li>')

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hermes Platform API Docs — {name}</title>
<style>
  :root {{ --bg: #0d1117; --surface: #161b22; --border: #30363d; --text: #c9d1d9;
           --heading: #f0f6fc; --accent: #58a6ff; --code-bg: #1c2128; --link: #58a6ff; }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
          background: var(--bg); color: var(--text); display: flex; min-height: 100vh; }}
  aside {{ width: 260px; min-width: 260px; background: var(--surface); border-right: 1px solid var(--border);
           padding: 24px 16px; position: sticky; top: 0; height: 100vh; overflow-y: auto; }}
  aside h2 {{ color: var(--heading); font-size: 16px; margin-bottom: 12px; padding: 0 8px; }}
  aside ul {{ list-style: none; }}
  aside li {{ margin-bottom: 2px; }}
  .nav-link {{ display: block; padding: 6px 12px; border-radius: 6px; color: var(--text);
               text-decoration: none; font-size: 14px; transition: background .15s; }}
  .nav-link:hover {{ background: rgba(88,166,255,.1); color: var(--accent); }}
  .nav-link.active {{ background: rgba(88,166,255,.15); color: var(--accent); font-weight: 600; }}
  main {{ flex: 1; max-width: 960px; margin: 0 auto; padding: 40px 32px 80px; }}
  h1 {{ color: var(--heading); font-size: 28px; margin: 32px 0 16px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }}
  h2 {{ color: var(--heading); font-size: 22px; margin: 28px 0 12px; border-bottom: 1px solid var(--border); padding-bottom: 8px; }}
  h3 {{ color: var(--heading); font-size: 18px; margin: 24px 0 8px; }}
  h4 {{ color: var(--heading); font-size: 15px; margin: 20px 0 6px; }}
  p {{ margin: 8px 0; line-height: 1.7; }}
  a {{ color: var(--link); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  table {{ border-collapse: collapse; margin: 12px 0; width: 100%; font-size: 14px; }}
  th, td {{ border: 1px solid var(--border); padding: 8px 12px; text-align: left; }}
  th {{ background: var(--surface); color: var(--heading); font-weight: 600; }}
  tr:nth-child(even) {{ background: rgba(22,27,34,.5); }}
  code {{ background: var(--code-bg); padding: 2px 6px; border-radius: 4px; font-size: 13px;
          font-family: 'SF Mono', 'Fira Code', Consolas, monospace; }}
  pre {{ background: var(--code-bg); border: 1px solid var(--border); border-radius: 8px;
         padding: 16px; margin: 12px 0; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; font-size: 13px; line-height: 1.6; }}
  blockquote {{ border-left: 3px solid var(--accent); margin: 12px 0; padding: 8px 16px;
                background: rgba(88,166,255,.05); }}
  ul, ol {{ margin: 8px 0; padding-left: 24px; }}
  li {{ margin: 4px 0; line-height: 1.6; }}
  hr {{ border: none; border-top: 1px solid var(--border); margin: 24px 0; }}
  @media (max-width: 768px) {{
    aside {{ display: none; }}
    main {{ padding: 20px 16px; }}
  }}
</style>
</head>
<body>
<aside>
  <h2>Hermes Docs</h2>
  <ul>{"".join(nav_items)}</ul>
</aside>
<main>{body}</main>
</body>
</html>"""
    return HTMLResponse(content=html)


@app.get("/docs")
async def docs_index():
    """Redirect to default doc."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/docs/api")


# ── Generic HTTP proxy catch-all (MUST be last /api/* route) ──

@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def proxy_api(request: Request, path: str):
    """
    Proxy /api/* to user's Hermes process.
    Auto-prefixes /api/ back when forwarding.
    """
    proc = await _proc_for_request(request)
    body = await request.body()
    ct = request.headers.get("content-type", "")
    if path == "config" and request.method == "PUT" and body:
        logger.info(f"PUT /api/config body (user={getattr(proc,'user_id','?')}): {body[:500]}")
    return await _hermes_http(proc, request.method, f"/api/{path}", body, ct)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("hermes_broker:app", host="0.0.0.0", port=8080, log_level="info")
