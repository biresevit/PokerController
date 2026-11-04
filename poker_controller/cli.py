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
from .sites import BLUFFPOKER, EVENBET, table_key
from .transport import redact


def parser():
    p = argparse.ArgumentParser(description="Standalone EvenBet / BluffPoker controller and PDecision bot")
    p.add_argument("command", choices=["tables", "observe", "seat", "play", "play-multi", "leave", "health"])
    p.add_argument("--site", choices=["evenbet", "bluffpoker"], default="evenbet")
    p.add_argument("--config", type=Path, help="BluffPoker config file (default C:\PokerSuite\bluffpoker.json)")
    p.add_argument("--email", default=os.getenv("BP_BOT_EMAIL"), help="BluffPoker login, overriding the config file")
    p.add_argument("--url", default="wss://web.evenbetpoker.com/front")
    p.add_argument("--origin", default="https://demo.evenbetpoker.com")
    p.add_argument("--skin", default="evenbetpoker")
    p.add_argument("--username", default=os.getenv("EVENBET_USERNAME", "monica"))
    p.add_argument("--password", default=os.getenv("EVENBET_PASSWORD", "poker"))
    p.add_argument("--table", type=table_key, default=113891676)
    p.add_argument("--tables", type=table_key, nargs="+", help="Table IDs for play-multi")
    p.add_argument("--buy-in", default="80", help="Chips, not minor currency units")
    p.add_argument("--seat", type=int, help="Zero-based seat; otherwise choose a free one")
    p.add_argument("--seconds", type=float, default=120)
    p.add_argument("--max-actions", type=int)
    p.add_argument("--decision-url", default="http://127.0.0.1:5005")
    p.add_argument("--api-key", default=os.getenv("PDECISION_API_KEY", ""))
    p.add_argument("--platform", type=int, help="enPlatform sent to PDecision (default: 11 EvenBet, 18 BluffPoker)")
    p.add_argument("--hand-history", action=argparse.BooleanOptionalAction, default=True,
                   help="Send completed hands to PDecision (default for play commands)")
    p.add_argument("--capture", type=Path, help="Save redacted server messages as JSONL")
    p.add_argument("--keep-seat", action="store_true", help="Keep seat after this command exits")
    p.add_argument("--debug", action="store_true")
    return p


def make_client(args):
    if args.site == "bluffpoker":
        from dataclasses import replace
        from .bluffpoker import BluffPokerClient, BluffPokerSettings
        settings = BluffPokerSettings.load(args.username, path=args.config)
        password = os.getenv("BP_BOT_PASSWORD") if args.email else None
        if args.email:
            settings = replace(settings, email=args.email, password=password or settings.password)
        return BluffPokerClient(settings)
    return EvenBetClient(Settings(url=args.url, origin=args.origin, skin=args.skin,
                                  username=args.username, password=args.password))


async def run(args):
    platform = BLUFFPOKER if args.site == "bluffpoker" else EVENBET
    decisions = PDecision(args.decision_url, args.api_key)
    try:
        if args.command == "health":
            print(json.dumps(await decisions.health(), indent=2))
            return
        # Check before spending chips or taking a seat.
        if args.command in ("play", "play-multi"):
            print("PDecision:", await decisions.health())
            if args.hand_history:
                await decisions.require_hand_history(platform)
        async with make_client(args) as client:
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
                        print(f"{t['i']!s:<11} {t['g']:<4} {t.get('np') or 0:<7} {t.get('mp') or 0:<6} {(t.get('mn') or 0)/100:<11g} {t['n']}")
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
                    bot = Bot(client, decisions, args.table, platform=args.platform or platform,
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
