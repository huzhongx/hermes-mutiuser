"""
WebSocket Connection Pool — v3.0

管理到所有 Hermes 进程的 WebSocket 长连接。

v3.0 特性：
- 自动重连（指数退避 + 最大重试次数）
- 心跳 ping/pong 检测静默断连
- 连接状态机（disconnected → connecting → connected）
- pending futures 超时清理
- JSON-RPC 自增 id
"""

import asyncio
import enum
import json
import logging
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


class ConnState(enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"


class WSConnection:
    """
    到单个 Hermes 进程的 WebSocket 连接（含自动重连 + 心跳）。
    """

    RECONNECT_BASE_DELAY = 1.0
    RECONNECT_MAX_DELAY = 30.0
    RECONNECT_MAX_RETRIES = 10
    HEARTBEAT_INTERVAL = 30.0
    HEARTBEAT_TIMEOUT = 10.0
    PENDING_CLEANUP_INTERVAL = 60.0
    RPC_TIMEOUT = 120

    def __init__(self, host: str, port: int, process_id: str, token: str = ""):
        self._base_host = host
        self._base_port = port
        self._token = token
        if token:
            self.url = f"http://{host}:{port}/api/ws?token={token}"
        else:
            self.url = f"http://{host}:{port}/api/ws"
        self.process_id = process_id

        self._state: ConnState = ConnState.DISCONNECTED
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._session: Optional[aiohttp.ClientSession] = None

        self._next_id = 0
        self._id_lock = asyncio.Lock()
        self._pending: Dict[str, asyncio.Future] = {}

        self._reader_task: Optional[asyncio.Task] = None
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        self._reconnect_count = 0
        self._closed_permanently = False

        self._last_pong_time = time.monotonic()

    @property
    def state(self) -> ConnState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return (
            self._state == ConnState.CONNECTED
            and self._ws is not None
            and not self._ws.closed
        )

    # ── 连接建立 ─────────────────────────────────────────

    async def connect(self):
        """首次建立连接并启动所有后台任务"""
        await self._do_connect()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._cleanup_task = asyncio.create_task(self._pending_cleanup_loop())
        logger.info(f"[{self.process_id}] WS 连接已建立: {self.url}")

    async def _do_connect(self):
        """底层连接建立"""
        self._state = ConnState.CONNECTING
        try:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            self._ws = await self._session.ws_connect(
                self.url,
                autoclose=False,
                heartbeat=None,
            )
            self._state = ConnState.CONNECTED
            self._reconnect_count = 0
            self._last_pong_time = time.monotonic()
        except Exception as e:
            self._state = ConnState.DISCONNECTED
            raise RuntimeError(f"WS 连接失败: {e}") from e

    # ── 读循环 ───────────────────────────────────────────

    async def _read_loop(self):
        """持续读取 WS 消息，断开时触发重连"""
        while not self._closed_permanently:
            if not self.is_connected:
                reconnected = await self._try_reconnect()
                if not reconnected:
                    break
                continue

            try:
                msg = await asyncio.wait_for(
                    self._ws.receive(), timeout=60
                )
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                logger.error(f"[{self.process_id}] WS 读取异常: {e}")
                self._state = ConnState.DISCONNECTED
                continue

            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_text(msg.data)
            elif msg.type == aiohttp.WSMsgType.PONG:
                self._last_pong_time = time.monotonic()
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING):
                logger.warning(f"[{self.process_id}] WS 连接被对端关闭")
                self._state = ConnState.DISCONNECTED
            elif msg.type == aiohttp.WSMsgType.ERROR:
                logger.error(f"[{self.process_id}] WS 错误: {msg.data}")
                self._state = ConnState.DISCONNECTED

    async def _handle_text(self, raw: str):
        try:
            data = json.loads(raw)
            req_id = str(data.get("id", ""))
            if req_id and req_id in self._pending:
                future = self._pending.pop(req_id)
                if not future.done():
                    future.set_result(data)
        except Exception as e:
            logger.error(f"[{self.process_id}] 解析 WS 消息失败: {e}")

    # ── 重连 ─────────────────────────────────────────────

    async def _try_reconnect(self) -> bool:
        if self._closed_permanently:
            return False

        self._reconnect_count += 1
        if self._reconnect_count > self.RECONNECT_MAX_RETRIES:
            logger.error(
                f"[{self.process_id}] 超过最大重连次数 "
                f"({self.RECONNECT_MAX_RETRIES})，放弃重连"
            )
            self._fail_all_pending(ConnectionError("WS 连接不可恢复"))
            return False

        delay = min(
            self.RECONNECT_BASE_DELAY * (2 ** (self._reconnect_count - 1)),
            self.RECONNECT_MAX_DELAY,
        )
        logger.warning(
            f"[{self.process_id}] WS 断开，{delay:.1f}s 后尝试第 "
            f"{self._reconnect_count} 次重连..."
        )
        await asyncio.sleep(delay)

        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
            await self._do_connect()
            logger.info(f"[{self.process_id}] WS 重连成功")
            return True
        except Exception as e:
            logger.error(f"[{self.process_id}] WS 重连失败: {e}")
            return False

    # ── 心跳检测 ─────────────────────────────────────────

    async def _heartbeat_loop(self):
        while not self._closed_permanently:
            await asyncio.sleep(self.HEARTBEAT_INTERVAL)
            if not self.is_connected:
                continue
            try:
                await self._ws.ping()
                if (
                    time.monotonic() - self._last_pong_time
                    > self.HEARTBEAT_INTERVAL + self.HEARTBEAT_TIMEOUT
                ):
                    logger.warning(f"[{self.process_id}] 心跳超时，标记连接断开")
                    self._state = ConnState.DISCONNECTED
            except Exception as e:
                logger.warning(f"[{self.process_id}] 心跳发送失败: {e}")
                self._state = ConnState.DISCONNECTED

    # ── pending futures 清理 ─────────────────────────────

    async def _pending_cleanup_loop(self):
        while not self._closed_permanently:
            await asyncio.sleep(self.PENDING_CLEANUP_INTERVAL)
            now = time.monotonic()
            stale = [
                rid for rid, fut in self._pending.items()
                if now - getattr(fut, '_created_at', now) > self.RPC_TIMEOUT * 2
            ]
            for rid in stale:
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    fut.set_exception(TimeoutError("RPC 请求超时清理"))
            if stale:
                logger.warning(
                    f"[{self.process_id}] 清理 {len(stale)} 个超时 pending futures"
                )

    # ── JSON-RPC 调用 ────────────────────────────────────

    async def call(
        self, method: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """发送 JSON-RPC 请求并等待响应"""
        if self._closed_permanently:
            raise ConnectionError("WS 连接已永久关闭")

        # 等待连接就绪
        for _ in range(30):
            if self.is_connected:
                break
            if self._closed_permanently:
                raise ConnectionError("WS 连接已永久关闭")
            await asyncio.sleep(1)
        else:
            raise ConnectionError("WS 连接未就绪，等待超时")

        async with self._id_lock:
            self._next_id += 1
            req_id = str(self._next_id)

        payload = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params:
            payload["params"] = params

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        future._created_at = time.monotonic()  # type: ignore[attr-defined]
        self._pending[req_id] = future

        try:
            await self._ws.send_str(json.dumps(payload))
        except Exception as e:
            self._pending.pop(req_id, None)
            self._state = ConnState.DISCONNECTED
            raise ConnectionError(f"WS 发送失败: {e}") from e

        try:
            result = await asyncio.wait_for(future, timeout=self.RPC_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise TimeoutError(f"RPC 调用超时 ({self.RPC_TIMEOUT}s): {method}")

        if "error" in result:
            raise RuntimeError(f"JSON-RPC error: {result['error']}")
        return result.get("result", {})

    # ── 关闭 ─────────────────────────────────────────────

    async def close(self):
        """永久关闭连接"""
        self._closed_permanently = True
        for task in (self._reader_task, self._heartbeat_task, self._cleanup_task):
            if task:
                task.cancel()
        self._fail_all_pending(ConnectionError("连接关闭"))
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._state = ConnState.DISCONNECTED
        logger.info(f"[{self.process_id}] WS 连接已永久关闭")

    def _fail_all_pending(self, exc: Exception):
        for rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()


class WSConnectionPool:
    """
    管理所有 Hermes 进程的 WebSocket 连接。
    session_id → WSConnection 映射。
    """

    def __init__(self):
        self._connections: Dict[str, WSConnection] = {}
        self._lock = asyncio.Lock()

    async def add(self, session_id: str, host: str, port: int, token: str = "") -> WSConnection:
        """为新进程建立 WS 连接（带 token）"""
        async with self._lock:
            conn = WSConnection(host, port, session_id, token=token)
            await conn.connect()
            self._connections[session_id] = conn
            return conn

    async def get(self, session_id: str) -> Optional[WSConnection]:
        conn = self._connections.get(session_id)
        if conn and not conn.is_connected and not conn._closed_permanently:
            logger.warning(f"[{session_id}] WS 连接断开中，等待重连...")
        return conn

    async def remove(self, session_id: str):
        """永久关闭并移除 WS 连接"""
        async with self._lock:
            conn = self._connections.pop(session_id, None)
            if conn:
                await conn.close()

    async def close_all(self):
        for sid in list(self._connections.keys()):
            await self.remove(sid)

    def stats(self) -> dict:
        connected = sum(1 for c in self._connections.values() if c.is_connected)
        return {
            "total": len(self._connections),
            "connected": connected,
            "disconnected": len(self._connections) - connected,
        }
