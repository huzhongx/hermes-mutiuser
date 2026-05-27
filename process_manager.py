"""
Hermes Process Manager — v3.1

适配 Hermes Agent v0.14.0+ 的进程管理器。
使用 `hermes dashboard --tui` 启动每个实例，通过 /api/ws JSON-RPC 通信。

关键设计：
- 每个 Hermes 进程以 `hermes dashboard --tui --skip-build` 模式运行
- 每个进程监听一个独立端口（端口范围 9119~9200，set 管理）
- 通过 WebSocket /api/ws?token=TOKEN 与进程通信（JSON-RPC）
- 每个 session 的 HERMES_HOME 独立目录实现数据隔离
- 预热池（Warm Pool）减少冷启动延迟
- 租户级并发限制
- 事务安全：创建失败时完整回滚
"""

import asyncio
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Set

import aiohttp

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────

@dataclass
class HermesProcess:
    """代表一个运行中的 Hermes 进程"""
    session_id: str
    tenant_id: str
    hermes_home: str
    port: int
    process: Optional[asyncio.subprocess.Process] = None
    pid: Optional[int] = None
    status: str = "initializing"
    ws_token: str = ""           # dashboard 注入到 HTML 的 ephemeral session token
    created_at: float = field(default_factory=time.time)
    last_active_at: float = field(default_factory=time.time)
    _log_file_handle = None

    def touch(self):
        self.last_active_at = time.time()

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_active_at


# ─────────────────────────────────────────────────────────
# 进程管理器
# ─────────────────────────────────────────────────────────

class ProcessManager:
    """
    管理所有租户的 Hermes 进程。

    v3.1 特性（适配 Hermes v0.14.0）：
    - 用 `python -m hermes_cli.main dashboard --tui --skip-build` 启动
    - 共享默认 HERMES_HOME 的模型配置/API key
    - 每个进程监听独立端口（set 管理）
    - WS 连接带 token（从 dashboard HTML 提取）
    - 预热池 / 租户级配额 / 事务安全
    """

    # Hermes dashboard 启动方式
    _HERMES_PYTHON = "/root/.hermes/hermes-agent/venv/bin/python"
    _HERMES_MODULE = "hermes_cli.main"

    PORT_RANGE_START = 9119
    PORT_RANGE_END = 9200

    def __init__(
        self,
        sessions_root: str = "/tmp/hermes_sessions",
        base_port: int = 9119,
        max_port: int = 9200,
        max_sessions: int = 80,
        idle_timeout: int = 1800,
        healthcheck_timeout: int = 60,
        warm_pool_size: int = 5,
        warm_pool_check_interval: int = 30,
        tenant_quotas: Optional[Dict[str, int]] = None,
    ):
        self.sessions_root = Path(sessions_root)
        self.base_port = base_port
        self.max_port = max_port
        self.max_sessions = max_sessions
        self.idle_timeout = idle_timeout
        self.healthcheck_timeout = healthcheck_timeout
        self.warm_pool_size = warm_pool_size
        self.warm_pool_check_interval = warm_pool_check_interval
        self.tenant_quotas: Dict[str, int] = tenant_quotas or {}

        self._processes: Dict[str, HermesProcess] = {}   # session_id → HermesProcess
        self._warm_pool: Dict[str, HermesProcess] = {}   # warm session_id → HermesProcess
        self._used_ports: Set[int] = set()
        self._available_ports: Set[int] = set(range(base_port, max_port + 1))
        self._lock = asyncio.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        self._warm_pool_task: Optional[asyncio.Task] = None
        self._http_session: Optional[aiohttp.ClientSession] = None

        # Hermes 配置目录（共享模型配置和 API key）
        self._hermes_config_home = Path(os.environ.get("HERMES_HOME", "/root/.hermes"))

    # ── 外部接口：租户配额管理 ─────────────────────────────

    def set_tenant_quota(self, tenant_id: str, max_sessions: int):
        """动态更新租户配额"""
        self.tenant_quotas[tenant_id] = max_sessions

    def get_tenant_active_count(self, tenant_id: str) -> int:
        """统计租户当前活跃会话数"""
        return sum(
            1 for p in self._processes.values()
            if p.tenant_id == tenant_id and p.status in ("initializing", "active", "idle")
        )

    # ── 生命周期 ──────────────────────────────────────────

    async def start(self):
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._http_session = aiohttp.ClientSession()
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._warm_pool_task = asyncio.create_task(self._warm_pool_maintainer())
        logger.info(
            f"ProcessManager started. sessions_root={self.sessions_root}, "
            f"warm_pool_target={self.warm_pool_size}"
        )

    async def stop(self):
        for task in (self._cleanup_task, self._warm_pool_task):
            if task:
                task.cancel()

        for sid in list(self._warm_pool.keys()):
            await self._destroy_warm_process(sid)

        for sid in list(self._processes.keys()):
            await self.close_session(sid, reason="manager_shutdown")

        if self._http_session:
            await self._http_session.close()

        logger.info("ProcessManager stopped.")

    # ── 端口分配（set-based O(1)）────────────────────────

    def _allocate_port(self) -> int:
        """从可用端口集合中弹出一个端口"""
        if not self._available_ports:
            raise RuntimeError("无可用端口，已达系统上限")
        port = min(self._available_ports)
        self._available_ports.discard(port)
        self._used_ports.add(port)
        return port

    def _release_port(self, port: int):
        """归还端口到可用集合"""
        self._used_ports.discard(port)
        self._available_ports.add(port)

    # ── 预热池（Warm Pool）────────────────────────────────

    async def _warm_pool_maintainer(self):
        """后台任务：维持预热池水位"""
        while True:
            try:
                await asyncio.sleep(self.warm_pool_check_interval)
                await self._replenish_warm_pool()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"预热池维护异常: {e}")

    async def _replenish_warm_pool(self):
        """补充预热池到目标水位"""
        current_warm = len(self._warm_pool)
        needed = self.warm_pool_size - current_warm
        if needed <= 0:
            return

        active_count = len(self._processes) + len(self._warm_pool)
        if active_count + needed > self.max_sessions:
            needed = max(0, self.max_sessions - active_count)

        for _ in range(needed):
            try:
                proc = await self._spawn_hermes_process(
                    tenant_id="__warm__",
                    session_id=f"__warm_{uuid.uuid4().hex[:8]}__",
                )
                proc.status = "warm"
                async with self._lock:
                    self._warm_pool[proc.session_id] = proc
                logger.info(
                    f"[warm] 预热进程就绪，port={proc.port}, "
                    f"pool_size={len(self._warm_pool)}"
                )
            except Exception as e:
                logger.error(f"[warm] 预热进程启动失败: {e}")

    async def _destroy_warm_process(self, session_id: str):
        """销毁一个预热进程"""
        async with self._lock:
            proc = self._warm_pool.pop(session_id, None)
        if not proc:
            return
        await self._kill_process(proc)
        self._release_port(proc.port)
        if proc.hermes_home and os.path.exists(proc.hermes_home):
            shutil.rmtree(proc.hermes_home, ignore_errors=True)

    # ── 会话创建 ─────────────────────────────────────────

    async def create_session(
        self,
        tenant_id: str,
        session_id: Optional[str] = None,
        model: str = "nous-hermes-3",
        system_prompt: Optional[str] = None,
    ) -> HermesProcess:
        """
        创建新的 Hermes 会话。

        策略：
        1. 检查全局并发上限 + 租户级配额
        2. 优先从预热池取一个已就绪的进程（<1s）
        3. 预热池为空时回退到冷启动
        4. 失败时完整回滚
        """
        # 容量检查
        async with self._lock:
            active_count = sum(
                1 for p in self._processes.values()
                if p.status in ("initializing", "active", "idle")
            )
            if active_count >= self.max_sessions:
                raise RuntimeError(f"已达到全局最大并发会话数 {self.max_sessions}")

        # 租户级配额检查
        tenant_limit = self.tenant_quotas.get(tenant_id)
        if tenant_limit is not None:
            tenant_count = self.get_tenant_active_count(tenant_id)
            if tenant_count >= tenant_limit:
                raise RuntimeError(
                    f"租户 {tenant_id} 已达配额上限 {tenant_limit} 个活跃会话"
                )

        if session_id is None:
            session_id = str(uuid.uuid4())

        # 尝试从预热池取进程
        proc = await self._try_take_warm(tenant_id, session_id)
        if proc is None:
            proc = await self._spawn_hermes_process(
                tenant_id=tenant_id,
                session_id=session_id,
            )

        proc.status = "active"
        async with self._lock:
            self._processes[session_id] = proc

        return proc

    async def _try_take_warm(self, tenant_id: str, session_id: str) -> Optional[HermesProcess]:
        """尝试从预热池取一个已就绪的进程"""
        async with self._lock:
            if not self._warm_pool:
                return None
            warm_sid = next(iter(self._warm_pool))
            proc = self._warm_pool.pop(warm_sid)

        proc.tenant_id = tenant_id
        proc.session_id = session_id

        actual_home = self.sessions_root / tenant_id / session_id
        actual_home.mkdir(parents=True, exist_ok=True)
        proc.hermes_home = str(actual_home)

        logger.info(f"[{session_id}] 从预热池分配进程，port={proc.port}（<1s）")
        return proc

    async def _spawn_hermes_process(
        self,
        tenant_id: str,
        session_id: str,
    ) -> HermesProcess:
        """
        底层：启动一个 hermes dashboard --tui 进程。
        失败时保证完整回滚。
        """
        async with self._lock:
            port = self._allocate_port()

        if tenant_id == "__warm__":
            hermes_home = self.sessions_root / "__warm__" / session_id
        else:
            hermes_home = self.sessions_root / tenant_id / session_id
        hermes_home.mkdir(parents=True, exist_ok=True)
        logs_dir = hermes_home / "logs"
        logs_dir.mkdir(exist_ok=True)

        proc = HermesProcess(
            session_id=session_id,
            tenant_id=tenant_id,
            hermes_home=str(hermes_home),
            port=port,
        )

        log_file_path = logs_dir / "session.log"
        proc_env = os.environ.copy()
        # 共享默认 HERMES_HOME 的模型配置和 API key
        proc_env["HERMES_HOME"] = str(self._hermes_config_home)

        log_fh = None
        try:
            log_fh = open(log_file_path, "a")

            logger.info(
                f"[{session_id}] 启动 Hermes dashboard --tui，port={port}, "
                f"HERMES_HOME={self._hermes_config_home}"
            )
            proc.process = await asyncio.create_subprocess_exec(
                self._HERMES_PYTHON, "-m", self._HERMES_MODULE,
                "dashboard",
                "--tui",
                "--skip-build",
                "--host", "127.0.0.1",
                "--port", str(port),
                "--no-open",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
                env=proc_env,
                cwd=str(hermes_home),
            )
            proc.pid = proc.process.pid
            proc._log_file_handle = log_fh
            log_fh = None  # 所有权转移
            logger.info(f"[{session_id}] Hermes 进程已启动，PID={proc.pid}")

            await asyncio.wait_for(
                self._wait_for_ready(proc),
                timeout=self.healthcheck_timeout,
            )
            logger.info(f"[{session_id}] Hermes 进程就绪，port={port}, token={proc.ws_token[:8]}...")

        except Exception as e:
            logger.error(f"[{session_id}] 进程启动失败: {type(e).__name__}: {e}")
            await self._kill_process(proc)
            async with self._lock:
                self._release_port(port)
            if hermes_home.exists():
                shutil.rmtree(hermes_home, ignore_errors=True)
            if log_fh:
                log_fh.close()
            proc.status = "error"
            raise RuntimeError(f"启动 Hermes 进程失败: {type(e).__name__}: {e}") from e

        return proc

    async def _wait_for_ready(self, proc: HermesProcess):
        """
        等待 Hermes dashboard 就绪并提取 WS token。

        流程：
        1. 轮询 HTTP GET / 直到返回有效 HTML
        2. 从 HTML 中提取 __HERMES_SESSION_TOKEN__
        3. 存入 proc.ws_token
        """
        base_url = f"http://127.0.0.1:{proc.port}"

        for attempt in range(45):  # 最多 90s
            await asyncio.sleep(2)
            try:
                async with aiohttp.ClientSession() as sess:
                    async with sess.get(
                        base_url + "/", timeout=aiohttp.ClientTimeout(total=5)
                    ) as resp:
                        if resp.status != 200:
                            logger.debug(f"[{proc.session_id}] HTTP {resp.status}, retry...")
                            continue
                        html = await resp.text()
                        m = re.search(r'__HERMES_SESSION_TOKEN__\s*=\s*"([^"]+)"', html)
                        if m:
                            proc.ws_token = m.group(1)
                            logger.info(
                                f"[{proc.session_id}] Token 提取成功，"
                                f"attempt={attempt+1}"
                            )
                            return
                        logger.debug(f"[{proc.session_id}] HTML 中无 token，retry...")
            except Exception as e:
                logger.debug(f"[{proc.session_id}] 连接失败: {e}")
                continue

        raise RuntimeError("Hermes dashboard 启动超时或 token 提取失败")

    # ── 会话关闭 ─────────────────────────────────────────

    async def close_session(self, session_id: str, reason: str = "explicit") -> bool:
        async with self._lock:
            proc = self._processes.get(session_id)
            if not proc:
                return False
            if proc.status == "closed":
                return True
            proc.status = "closing"

        logger.info(f"[{session_id}] 关闭会话，原因: {reason}")

        await self._kill_process(proc)
        proc.status = "closed"

        if proc._log_file_handle:
            try:
                proc._log_file_handle.close()
            except Exception:
                pass

        async with self._lock:
            self._processes.pop(session_id, None)

        async with self._lock:
            self._release_port(proc.port)

        return True

    async def _kill_process(self, proc: HermesProcess):
        p = proc.process
        if p is None or p.returncode is not None:
            return
        try:
            p.terminate()
            await asyncio.wait_for(p.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                p.kill()
            except ProcessLookupError:
                pass
        except ProcessLookupError:
            pass
        logger.info(f"[{proc.session_id}] 进程 PID={proc.pid} 已终止")

    # ── 查询 ─────────────────────────────────────────────

    def get_process(self, session_id: str) -> Optional[HermesProcess]:
        return self._processes.get(session_id)

    def list_processes(self, tenant_id: Optional[str] = None):
        processes = list(self._processes.values())
        if tenant_id:
            processes = [p for p in processes if p.tenant_id == tenant_id]
        return processes

    def stats(self) -> dict:
        all_procs = list(self._processes.values())
        status_counts: Dict[str, int] = {}
        for p in all_procs:
            status_counts[p.status] = status_counts.get(p.status, 0) + 1
        return {
            "total_active": len(all_procs),
            "warm_pool_size": len(self._warm_pool),
            "available_ports": len(self._available_ports),
            "max_sessions": self.max_sessions,
            "by_status": status_counts,
        }

    # ── 后台清理 ─────────────────────────────────────────

    async def _cleanup_loop(self):
        while True:
            try:
                await asyncio.sleep(60)
                await self._cleanup_idle_sessions()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"清理任务异常: {e}")

    async def _cleanup_idle_sessions(self):
        to_close = []
        async with self._lock:
            for sid, proc in self._processes.items():
                if proc.status == "idle" and proc.idle_seconds > self.idle_timeout:
                    to_close.append(sid)

        for sid in to_close:
            logger.info(f"[{sid}] 空闲超时（{self.idle_timeout}s），自动回收")
            await self.close_session(sid, reason="idle_timeout")
