"""Run it twice on BluffPoker (docs/RIT-technical-sheet-EN.md): the offer reaches the pilot, the bot's answer goes
back on the table socket, and a two-board showdown is played on instead of dropping the account."""
import copy
import json
from pathlib import Path

import pytest

from poker_controller import bluffpoker as bp

DATA = json.loads((Path(__file__).parent / "fixtures/bluffpoker.json").read_text())
PID = DATA["player_id"]
TABLE = "3b355673-ab0e-44f8-92cc-88021c2b03b8"


def client():
    c = bp.BluffPokerClient(bp.BluffPokerSettings(username="bot", hub_url="https://hub.test", api_key="k",
                                                  origin="https://test"))
    c.player_id = PID
    s = bp.TableSocket(c, TABLE, TABLE)
    s.ws = object()
    s.sent = []

    async def send(message):
        s.sent.append(message)
        return True
    s.send = send
    c.sockets[TABLE] = s
    return c, s


async def test_offer_reaches_the_pilot_and_the_answer_names_the_hand():
    c, s = client()
    s.handle({"type": "run_it_twice_offer", "tableId": TABLE, "handSeq": 42, "timeoutMs": 15000, "totalMs": 15000})
    assert await c.connection.next_event() == {"t": "RunItTwiceOffer", "tableId": TABLE, "handSeq": 42,
                                               "timeoutMs": 15000}
    assert await c.player_command(TABLE, 17) == {"ok": True}
    assert s.sent == [{"type": "run_it_twice_response", "tableId": TABLE, "accepted": True, "handSeq": 42}]
    assert await c.player_command(TABLE, 17) == {}  # answered once only
    assert len(s.sent) == 1


async def test_decline_and_a_closed_vote():
    c, s = client()
    s.handle({"type": "run_it_twice_offer", "tableId": TABLE, "handSeq": 7, "timeoutMs": 15000})
    await c.player_command(TABLE, 18)
    assert s.sent[-1]["accepted"] is False
    s.handle({"type": "run_it_twice_offer", "tableId": TABLE, "handSeq": 8, "timeoutMs": 15000})
    s.handle({"type": "run_it_twice_outcome", "tableId": TABLE, "handSeq": 8, "accepted": False, "reason": "timeout"})
    assert await c.player_command(TABLE, 17) == {} and len(s.sent) == 1
    s.handle({"type": "run_it_twice_offer", "tableId": "another-table", "handSeq": 9})
    assert s.rit_hand is None  # a late broadcast of another table


def showdown(**changes):
    raw = copy.deepcopy(DATA["states"]["preflop_turn"])
    raw.update(phase="showdown", currentActorSeat=None, board=["As", "7d", "2c", "Kh", "9s"],
               boardB=["As", "7d", "2c", "4c", "Jd"], **changes)
    return raw


def test_two_board_showdown_reports_board_a():
    s = bp.game_state(showdown(runItTwice={"sharedCards": 3}), PID, key=TABLE)
    assert s["ts"] == 3 and s["d"]["c"].count(";") == 4
    assert s["d"]["c"] == bp.card_list(["As", "7d", "2c", "Kh", "9s"])


def test_second_board_without_run_it_twice_is_still_refused():
    with pytest.raises(bp.BluffPokerError) as error:
        bp.game_state(showdown(), PID, key=TABLE)
    assert error.value.code == "UNSUPPORTED_FORMAT_secondBoard" and error.value.terminal
