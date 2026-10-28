"""Run independent table bots over one EvenBet login and WebSocket connection."""
from __future__ import annotations

import asyncio
import logging
import time

from .bot import Bot
from .client import EvenBetClient
from .decision import PDecision


log = logging.getLogger(__name__)


async def run_tables(client: EvenBetClient, decisions: PDecision, table_ids: list[int],
                     *, buy_in: str = "80", seconds: float = 1800,
                     hand_history: bool = False) -> dict[int, Bot]:
    await decisions.health()
    await client.list_tables()
    bots: dict[int, Bot] = {}
    joined: set[int] = set()
    workers: dict[int, asyncio.Task] = {}
    inboxes: dict[int, asyncio.Queue] = {}

    async def worker(table_id: int):
        try:
            while True:
                await bots[table_id].process(await inboxes[table_id].get())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Bot stopped at table %s", table_id)
            try:
                await client.leave(table_id)
                joined.discard(table_id)
                log.info("Left failed table %s", table_id)
            except Exception:
                log.exception("Could not leave failed table %s", table_id)

    try:
        for table_id in dict.fromkeys(table_ids):
            info = client.tables.get(table_id, {})
            if info.get("g") not in (72, 79) or info.get("p") not in (80, 85):
                log.warning("Skipping unsupported table %s", table_id)
                continue
            try:
                await client.join(table_id)
                joined.add(table_id)
                limits = await client.buy_in_range(table_id)
                minimum, maximum = limits["minAmount"], limits["maxAmount"]
                requested = round(float(buy_in) * 100)
                amount = min(max(requested, minimum), maximum) / 100
                seat = await client.sit(table_id, str(amount))
                bots[table_id] = Bot(client, decisions, table_id,
                                     hand_history=hand_history)
                inboxes[table_id] = asyncio.Queue(maxsize=1)
                log.info("Seated at table %s (%s), seat %s, buy-in %s", table_id,
                         "NLH" if info["g"] == 72 else "PLO4", seat, amount)
            except Exception:
                log.exception("Could not seat at table %s", table_id)
                if table_id in joined:
                    try:
                        await client.leave(table_id)
                    finally:
                        joined.discard(table_id)

        if not bots:
            raise RuntimeError("Could not seat at any selected table")
        workers = {table_id: asyncio.create_task(worker(table_id)) for table_id in bots}
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and any(not task.done() for task in workers.values()):
            try:
                event = await asyncio.wait_for(client.connection.next_event(),
                                               min(1, max(0.01, deadline - time.monotonic())))
            except TimeoutError:
                continue
            if event.get("t") == "GameState":
                state = event.get("gameState", event.get("GameState", {}))
                table_id = state.get("ti")
                if table_id in inboxes and not workers[table_id].done():
                    queue = inboxes[table_id]
                    if queue.full():
                        queue.get_nowait()  # Process the newest complete snapshot.
                    queue.put_nowait(state)
            elif event.get("t") == "NotifyFromServer" and event.get("resultCode"):
                log.error("Server notification: %s", event.get("text", event.get("resultCode")))
        return bots
    finally:
        for task in workers.values():
            task.cancel()
        if workers:
            await asyncio.gather(*workers.values(), return_exceptions=True)
        for table_id in list(joined):
            try:
                await client.leave(table_id)
                log.info("Left table %s", table_id)
            except Exception:
                log.exception("Could not leave table %s", table_id)
