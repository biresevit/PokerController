"""Opt-in checks against the actual demo. Never run in the normal test suite."""
import os

import pytest

from poker_controller.client import EvenBetClient

pytestmark = pytest.mark.skipif(os.getenv("POKER_LIVE") != "1", reason="Set POKER_LIVE=1 for demo integration tests")


async def test_demo_login_browse_join_buyin_leave():
    table_id = int(os.getenv("POKER_LIVE_TABLE", "113891676"))
    async with EvenBetClient() as client:
        assert client.player_id and client.player_id > 0
        tables = await client.list_tables()
        assert any(t["i"] == table_id for t in tables)
        joined = False
        try:
            await client.join(table_id)
            joined = True
            limits = await client.buy_in_range(table_id)
            seat = await client.sit(table_id, str(limits["minAmount"] / 100))
            state = await client.wait_state(table_id)
            assert state["s"][seat]["i"] == client.player_id
            assert state["s"][seat]["c"] > 0
        finally:
            if joined:
                await client.leave(table_id)
