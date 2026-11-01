import json

import pytest
from websockets.asyncio.server import serve

from poker_controller.bot import Bot
from poker_controller.client import EvenBetClient, Settings, hand_results


# Events as EvenBet sends them with the final GameState of a hand (captured 2026-10-26).
SHOWDOWN = [{"t": 6, "s": 4}, {"t": 7, "f": 1007, "ef": 25, "s": 4},
            {"t": 15, "s": 4, "m": '<nick s="4">AmBush</nick> wins  (<money a="1007" mt="P">10.07</money>) with '
                                   '<combination ch="H.36;17;18;7;6.H.H">Two Pair 6 3</combination>'},
            {"t": 12, "f": 1007, "ef": 1007, "s": 4}]
NOT_SHOWN = [{"t": 15, "m": '<nick s="0">Elizabeth</nick> wins  (<money a="2734" mt="P">27.34</money>): cards not shown.'}]


def test_hand_results_read_the_tables_win_announcements():
    assert hand_results(SHOWDOWN) == [{"seat": 4, "name": "AmBush", "amount": 10.07, "hand": "Two Pair 6 3",
                                       "text": "AmBush wins (10.07) with Two Pair 6 3"}]
    assert hand_results(NOT_SHOWN)[0] | {"text": ""} == {"seat": 0, "name": "Elizabeth", "amount": 27.34,
                                                         "hand": None, "text": ""}
    assert hand_results([{"t": 9, "a": 2, "s": 1}]) == [] and hand_results(None) == []


def test_client_keeps_every_pot_of_the_latest_hand():
    c = EvenBetClient()
    def receive(hand, events):
        c._on_message({"t": "GameState", "gameState": {"ti": 7, "gi": hand, "s": []}, "events": events})
    receive(1, SHOWDOWN)
    receive(1, NOT_SHOWN)  # A side pot announced in a later snapshot of the same hand.
    receive(1, SHOWDOWN)   # Repeated snapshots must not repeat a pot.
    assert [w["name"] for w in c.results[7]["winners"]] == ["AmBush", "Elizabeth"]
    receive(2, [])
    assert c.results[7]["hand_id"] == 1  # Kept on screen until the next hand has a result.
    receive(2, NOT_SHOWN)
    assert c.results[7] == {"hand_id": 2, "winners": hand_results(NOT_SHOWN)}


class Busts:
    def __init__(self):
        self.tables = []
    def __call__(self, table_id):
        self.tables.append(table_id)


def bot_at(chips, bet=0, dealt=None, ts=1, away=False):
    c = EvenBetClient()
    c.player_id = 42
    state = {"ti": 7, "gi": 5, "gt": 72, "ts": ts, "m": {"ai": 255, "di": 1, "r": 1}, "d": {"c": "", "p": 0},
             "s": [{"i": 42, "n": "monica", "c": chips, "b": bet, "d": dealt, "f": 1 if away else 0},
                   {"i": 9, "n": "villain", "c": 5000, "b": 0, "d": None, "f": 0}]}
    c.states[7] = state
    busts = Busts()
    bot = Bot(c, None, 7, on_bust=busts)
    return bot, state, busts


async def test_bot_reports_a_lost_stack_once():
    bot, state, busts = bot_at(0)
    await bot.process(state)
    await bot.process(state)
    assert busts.tables == [7]
    state["s"][0]["c"] = 8000  # Rebought.
    await bot.process(state)
    state["s"][0]["c"] = 0
    await bot.process(state)
    assert busts.tables == [7, 7]


@pytest.mark.parametrize("chips,bet,dealt,ts", [(0, 0, "8;9", 1), (0, 500, None, 1), (100, 0, None, 1)])
async def test_bot_does_not_report_all_in_or_funded_seats(chips, bet, dealt, ts):
    bot, state, busts = bot_at(chips, bet, dealt, ts)
    await bot.process(state)
    assert busts.tables == []


async def test_lost_showdown_is_a_bust_and_leaving_is_not():
    bot, state, busts = bot_at(0, dealt="8;9", ts=3)
    await bot.process(state)
    assert busts.tables == [7]
    bot, state, busts = bot_at(0)
    bot.leaving = True
    await bot.process(state)
    assert busts.tables == []


async def test_busted_bot_does_not_press_im_back():
    bot, state, busts = bot_at(0, away=True)
    pressed = []
    async def sit_back(table_id):
        pressed.append(table_id)
    bot.client.sit_back = sit_back
    await bot.process(state)
    assert busts.tables == [7] and pressed == []


@pytest.mark.parametrize("mid_hand", [False, True])
async def test_rebuy_buys_into_the_heros_own_seat(mid_hand):
    seen = []
    async def handler(ws):
        async for raw in ws:
            m = json.loads(raw)
            seen.append(m)
            reply = {"t": "Result", "srcMsgId": m["id"]}
            if m["t"] == "Login":
                reply.update(t="LoginResult", resultCode=2, playerId=42)
            elif m["t"] == "TableSitDownBuyInRangeRequest":
                reply.update(minAmount=4000, maxAmount=20000)
            elif m["t"] == "SitDown":
                if mid_hand:
                    reply.update(resultCode=1, text="Money will be added after the hand ends")
                await ws.send(json.dumps({"t": "GameState", "gameState": {"ti": 7,
                    "s": [None, {"i": 42, "n": "monica", "c": m["cash"]}]}}))
            await ws.send(json.dumps(reply))
    async with serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with EvenBetClient(Settings(url=f"ws://127.0.0.1:{port}")) as c:
            c.states[7] = {"ti": 7, "s": [None, {"i": 42, "n": "monica", "c": 0}]}
            with pytest.raises(ValueError):
                await c.rebuy(7, "300")
            assert await c.rebuy(7, "100", wait_big_blind=True) == 1
    sit = [m for m in seen if m["t"] == "SitDown"]
    assert len(sit) == 1 and sit[0]["seatIdx"] == 1 and sit[0]["cash"] == 10000 and 2 in sit[0]["playerCommands"]
    assert not any(m["t"] == "ReserveSeat" for m in seen)


async def test_bot_stays_away_during_a_planned_break():
    bot, state, busts = bot_at(5000, away=True)
    pressed = []
    async def sit_back(table_id):
        pressed.append(table_id)
    bot.client.sit_back = sit_back
    bot.on_break = True
    await bot.process(state)
    assert pressed == []
    bot.on_break = False
    await bot.process(state)
    assert pressed == [7]


async def test_bot_reports_a_show_offer_once_per_hand():
    bot, state, busts = bot_at(9000)
    offers = []
    bot.on_show_offer = lambda tid, s, hero: offers.append((tid, s["gi"], hero))
    state.update(ts=2, aa=[10, 16])
    state["m"].update(ai=0, r=5)
    await bot.process(state)
    await bot.process(state)
    assert offers == [(7, 5, 0)]
    state["m"]["ai"] = 1  # Offered to the other player: not ours.
    state["gi"] = 6
    await bot.process(state)
    assert offers == [(7, 5, 0)]
