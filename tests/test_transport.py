import asyncio
import json

import pytest
from websockets.asyncio.server import serve

from poker_controller.client import EvenBetClient, Settings
from poker_controller.transport import Connection, ProtocolError


async def test_transport_correlates_out_of_order_replies_and_notifications():
    async def handler(ws):
        first, second = json.loads(await ws.recv()), json.loads(await ws.recv())
        await ws.send(json.dumps({"t": "GameState", "gameState": {"ti": 1}}))
        for m in (second, first):
            await ws.send(json.dumps({"t": "Result", "srcMsgId": m["id"], "text": m["t"]}))
        await ws.wait_closed()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Connection(f"ws://127.0.0.1:{port}", "https://demo.evenbetpoker.com") as c:
            a, b = await asyncio.gather(c.request("One"), c.request("Two"))
            assert a["text"] == "One" and b["text"] == "Two"
            assert (await c.next_event())["t"] == "GameState"


async def test_disconnect_releases_pending_request():
    async def handler(ws):
        await ws.recv()
        await ws.close()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Connection(f"ws://127.0.0.1:{port}", "https://example.test") as c:
            with pytest.raises(ConnectionError):
                await c.request("NeverReplies")
            assert not c.pending


async def test_timeout_removes_request():
    async def handler(ws):
        await ws.recv()
        await ws.wait_closed()
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with Connection(f"ws://127.0.0.1:{port}", "https://example.test", timeout=.1) as c:
            with pytest.raises(TimeoutError):
                await c.request("NeverReplies")
            assert not c.pending


async def test_full_client_lifecycle_against_local_server():
    seen = []
    async def handler(ws):
        async for raw in ws:
            m = json.loads(raw)
            seen.append(m)
            reply = {"t": "Result", "srcMsgId": m["id"]}
            if m["t"] == "Login":
                reply.update(t="LoginResult", resultCode=2, playerId=42)
            elif m["t"] == "GetTablesList":
                reply.update(t="TablesList", tables=[{"i": 1, "n": "Test", "g": 72}])
            elif m["t"] == "SelectTable":
                await ws.send(json.dumps({"t": "GameState", "gameState": {"ti": 1, "s": [None, None]}}))
            elif m["t"] == "TableSitDownBuyInRangeRequest":
                reply.update(minAmount=4000, maxAmount=10000)
            elif m["t"] == "SitDown":
                assert m["cash"] == 8000
                await ws.send(json.dumps({"t": "GameState", "gameState": {"ti": 1,
                    "s": [{"i": 42, "n": "monica", "c": m["cash"]}, None]}}))
            await ws.send(json.dumps(reply))
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EvenBetClient(Settings(url=f"ws://127.0.0.1:{port}")) as c:
            assert c.player_id == 42
            assert (await c.list_tables())[0]["n"] == "Test"
            await c.join(1)
            with pytest.raises(ValueError):
                await c.sit(1, "200")
            assert await c.sit(1, "80") == 0
            assert await c.sit(1, "80") == 0
            await c.play(1, 1)
            await c.leave(1)
    assert sum(m["t"] == "SitDown" for m in seen) == 1
    assert any(m["t"] == "LeaveTable" for m in seen)
