from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .bot import Bot
from .client import EvenBetClient, Settings
from .decision import PDecision
from .multitable import run_tables
from .transport import redact


def parser():
    p = argparse.ArgumentParser(description="Standalone EvenBet demo controller and PDecision bot")
    p.add_argument("command", choices=["tables", "observe", "seat", "play", "play-multi", "leave", "health"])
    p.add_argument("--url", default="wss://web.evenbetpoker.com/front")
    p.add_argument("--origin", default="https://demo.evenbetpoker.com")
    p.add_argument("--skin", default="evenbetpoker")
    p.add_argument("--username", default=os.getenv("EVENBET_USERNAME", "monica"))
    p.add_argument("--password", default=os.getenv("EVENBET_PASSWORD", "poker"))
    p.add_argument("--table", type=int, default=113891676)
    p.add_argument("--tables", type=int, nargs="+", help="Table IDs for play-multi")
    p.add_argument("--buy-in", default="80", help="Chips, not minor currency units")
    p.add_argument("--seat", type=int, help="Zero-based seat; otherwise choose a free one")
    p.add_argument("--seconds", type=float, default=120)
    p.add_argument("--max-actions", type=int)
    p.add_argument("--decision-url", default="http://127.0.0.1:5005")
    p.add_argument("--api-key", default=os.getenv("PDECISION_API_KEY", ""))
    p.add_argument("--platform", type=int, default=11, help="EvenBet platform enum; PDecision verifies its machine registration")
    p.add_argument("--hand-history", action=argparse.BooleanOptionalAction, default=True,
                   help="Send completed hands to PDecision (default for play commands)")
    p.add_argument("--capture", type=Path, help="Save redacted server messages as JSONL")
    p.add_argument("--keep-seat", action="store_true", help="Keep seat after this command exits")
    p.add_argument("--debug", action="store_true")
    return p


async def run(args):
    decisions = PDecision(args.decision_url, args.api_key)
    try:
        if args.command == "health":
            print(json.dumps(await decisions.health(), indent=2))
            return
        # Check before spending chips or taking a seat.
        if args.command in ("play", "play-multi"):
            print("PDecision:", await decisions.health())
            if args.hand_history:
                await decisions.require_evenbet_hand_history()
        settings = Settings(url=args.url, origin=args.origin, skin=args.skin,
                            username=args.username, password=args.password)
        async with EvenBetClient(settings) as client:
            print(f"Logged in as {args.username} (player {client.player_id})")
            capture = None
            if args.capture:
                args.capture.parent.mkdir(parents=True, exist_ok=True)
                capture = args.capture.open("a", encoding="utf-8")
                def record(message):
                    capture.write(json.dumps(redact(message)) + "\n")
                    capture.flush()
                client.connection.listeners.append(record)
            joined = False
            try:
                if args.command == "play-multi":
                    if not args.tables:
                        raise ValueError("play-multi requires --tables")
                    bots = await run_tables(client, decisions, args.tables,
                                            buy_in=args.buy_in, seconds=args.seconds,
                                            hand_history=args.hand_history)
                    print("Completed:", {table_id: bot.actions for table_id, bot in bots.items()})
                    return
                tables = await client.list_tables()
                if args.command == "tables":
                    print("ID          GAME PLAYERS SEATS  MIN BUY-IN  NAME")
                    for t in tables:
                        print(f"{t['i']:<11} {t['g']:<4} {t.get('np',0):<7} {t.get('mp',0):<6} {t.get('mn',0)/100:<11g} {t['n']}")
                    return
                await client.join(args.table)
                joined = True
                if args.command == "leave":
                    print("Leave:", await client.leave(args.table))
                    joined = False
                    return
                print("Buy-in range:", await client.buy_in_range(args.table))
                if args.command in ("seat", "play"):
                    if args.command == "play":
                        info = client.tables[args.table]
                        if info.get("g") not in (72, 79, 88) or info.get("p") not in (80, 85):
                            raise ValueError("Play supports NL/PL Holdem and Omaha cash tables")
                    seat = await client.sit(args.table, args.buy_in, args.seat)
                    print(f"Seated at {seat}, table {args.table}")
                if args.command == "play":
                    bot = Bot(client, decisions, args.table, platform=args.platform,
                              hand_history=args.hand_history, max_actions=args.max_actions)
                    await bot.run(args.seconds)
                    print(f"Completed {bot.actions} PDecision actions")
                elif args.command == "observe":
                    print(json.dumps(client.states[args.table], indent=2))
                    async with asyncio.timeout(args.seconds):
                        while True:
                            event = await client.connection.next_event()
                            if event.get("t") == "GameState":
                                state = event.get("gameState", {})
                                if state.get("ti") == args.table:
                                    print(json.dumps(state))
            except TimeoutError:
                if args.command != "observe":
                    raise
            finally:
                try:
                    if joined and not args.keep_seat:
                        await asyncio.shield(client.leave(args.table))
                        print("Left table")
                finally:
                    if capture:
                        capture.close()
    finally:
        await decisions.close()


def main():
    args = parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    # websockets DEBUG dumps raw credential frames; application traces are redacted.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Stopped")
    except Exception as exc:
        logging.error("%s: %s", type(exc).__name__, exc)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
