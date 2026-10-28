"""One receive loop, correlated replies, independent server notifications."""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import Callable
from typing import Any

from websockets.asyncio.client import connect

log = logging.getLogger(__name__)
SENSITIVE = {"password", "auth", "oldauth", "logintoken", "tfaauth", "token"}


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: "<redacted>" if k.lower() in SENSITIVE else redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    return value


class ProtocolError(RuntimeError):
    def __init__(self, message: dict):
        self.response = message
        super().__init__(f"{message.get('t')}: {message.get('text', '')} "
                         f"(code={message.get('resultCode')}, ext={message.get('resultCodeExt')})")


def ensure_success(message: dict) -> dict:
    # EvenBet omits resultCode on normal success; LoginResult uses 2.
    if message.get("resultCode", 0) not in (0, 2):
        raise ProtocolError(message)
    return message


class Connection:
    def __init__(self, url: str, origin: str, timeout: float = 20):
        self.url, self.origin, self.timeout = url, origin, timeout
        self.socket = None
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue[dict | Exception] = asyncio.Queue(maxsize=4096)
        self.listeners: list[Callable[[dict], None]] = []
        self.sequence = 1000
        self.receiver: asyncio.Task | None = None
        self.failure: Exception | None = None

    async def open(self):
        self.socket = await connect(self.url, origin=self.origin, open_timeout=self.timeout,
                                    ping_interval=20, ping_timeout=20, max_size=16 * 1024 * 1024)
        self.receiver = asyncio.create_task(self._receive(), name="evenbet-receiver")
        return self

    async def request(self, topic: str, **fields) -> dict:
        if self.failure:
            raise self.failure
        if self.socket is None:
            raise RuntimeError("Connection is not open")
        self.sequence += 1
        request_id = self.sequence
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        message = dict(fields, t=topic, id=request_id)
        try:
            log.debug("send %s", redact(message))
            await self.socket.send(json.dumps(message, separators=(",", ":")))
            return ensure_success(await asyncio.wait_for(future, self.timeout))
        finally:
            self.pending.pop(request_id, None)

    async def send(self, topic: str, **fields) -> int:
        """Send a command whose server acknowledgement is an uncorrelated event."""
        if self.failure:
            raise self.failure
        if self.socket is None:
            raise RuntimeError("Connection is not open")
        self.sequence += 1
        request_id = self.sequence
        message = dict(fields, t=topic, id=request_id)
        log.debug("send %s", redact(message))
        await self.socket.send(json.dumps(message, separators=(",", ":")))
        return request_id

    async def _receive(self):
        try:
            async for raw in self.socket:
                message = json.loads(raw)
                log.debug("recv %s", redact(message))
                for listener in self.listeners:
                    listener(message)
                future = self.pending.get(message.get("srcMsgId"))
                if future is not None and not future.done():
                    future.set_result(message)
                # Replies don't need to occupy the notification queue.
                if "srcMsgId" not in message:
                    self.notifications.put_nowait(message)
            raise ConnectionError("EvenBet closed the connection")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.failure = exc
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(exc)
            # Overflow must terminate the session rather than silently dropping poker state.
            if self.notifications.full():
                self.notifications.get_nowait()
            self.notifications.put_nowait(exc)

    async def next_event(self) -> dict:
        if self.failure and self.notifications.empty():
            raise self.failure
        item = await self.notifications.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def close(self):
        if self.socket:
            await self.socket.close()
        if self.receiver:
            self.receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.receiver
        for future in self.pending.values():
            if not future.done():
                future.cancel()

    async def __aenter__(self):
        return await self.open()

    async def __aexit__(self, *_):
        await self.close()
