import copy
import json
from pathlib import Path

import httpx
import pytest

from poker_controller.bot import Bot
from poker_controller.cli import parser
from poker_controller.client import EvenBetClient
from poker_controller.decision import PDecision


def test_play_sends_histories_by_default_but_can_disable():
    assert parser().parse_args(["play"]).hand_history
    assert not parser().parse_args(["play", "--no-hand-history"]).hand_history


class FakeDecisions:
    def __init__(self, after=None):
        self.frames = []
        self.after = after
    async def decide(self, frame):
        self.frames.append(frame)
        if self.after:
            self.after()
        return {"Action": 1}


async def test_bot_submits_one_action_for_duplicate_snapshots(state):
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    decisions = FakeDecisions()
    bot = Bot(c, decisions, state["ti"])
    await bot.process(state)
    await bot.process(copy.deepcopy(state))
    assert played == [(state["ti"], 3, 100)]
    assert decisions.frames[0]["IsNewHand"]


@pytest.mark.parametrize('format',['SNG','MTT'])
async def test_tournament_bot_sends_native_handstate_and_correct_raise(state,format):
    from unittest.mock import AsyncMock
    c=EvenBetClient();c.player_id=73582719
    c.states[state['ti']]=state
    c.tables[state['ti']]={'tournament':{'format':format}}
    c.play=AsyncMock()
    decisions=type('Decisions',(),{'decide':AsyncMock(return_value={'Action':4,'BetSize':600})})()
    await Bot(c,decisions,state['ti']).process(state)
    sent=decisions.decide.await_args.args[0]
    assert sent['BBSize']==state['bbv'] and sent['Stacks'][0]==state['s'][2]['c']
    c.play.assert_awaited_once_with(state['ti'],9,500)


async def test_table_detachment_while_deciding_prevents_old_table_action(state):
    from unittest.mock import AsyncMock
    c=EvenBetClient();c.player_id=73582719;c.states[state['ti']]=state;c.play=AsyncMock()
    decisions=FakeDecisions();bot=Bot(c,decisions,state['ti'])
    decisions.after=lambda:setattr(bot,'table_detached',True)
    await bot.process(state)
    c.play.assert_not_awaited()


async def test_bot_discards_decision_when_turn_changes_during_http(state):
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = copy.deepcopy(state)
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    decisions = FakeDecisions(lambda: c.states[state["ti"]]["m"].update(ai=4))
    bot = Bot(c, decisions, state["ti"])
    await bot.process(state)
    assert not played


async def test_rest_adapter_posts_handstate_and_checks_health():
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path == "/api/health":
            return httpx.Response(200, json={"status": "ready"})
        if request.url.path in ("/api/handhistory", "/api/handhistory/complete"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"Action": 0, "BetSize": 0})
    http = httpx.AsyncClient(base_url="http://pdecision.test", transport=httpx.MockTransport(handler))
    api = PDecision(client=http)
    try:
        await api.health()
        assert (await api.decide({"TableID": "1"}))["Action"] == 0
        await api.record({"TableID": "1"})
        assert requests[1].content == b'{"TableID":"1"}'
        assert await api.complete({"TableID": "1"})
        assert requests[-1].url.path == "/api/handhistory/complete"
    finally:
        await api.close()


async def test_hand_history_waits_for_payout_and_sends_evenbet_platform(state):
    state["m"]["ai"] = 255
    state["m"]["r"] = 1
    state["aa"] = []
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    class Recorder:
        def __init__(self):
            self.frames = []
        async def record(self, frame):
            self.frames.append(frame)
            return True
        async def complete(self, frame):
            self.completed = frame
            return True
    decisions = Recorder()
    bot = Bot(c, decisions, state["ti"], hand_history=True)
    await bot.process(state)
    assert not decisions.frames
    ended = copy.deepcopy(state)
    ended["ts"] = 3
    ended["m"]["r"] = 6
    ended["s"][2]["c"] += 500
    await bot.process(ended)
    assert len(decisions.frames) == 2
    assert decisions.frames[0]["Platform"] == 11
    assert decisions.frames[-1]["Winners"][0] == 5
    assert decisions.completed["HandID"] == str(state["gi"])
    await bot.process(ended)
    assert len(decisions.frames) == 2


async def test_hand_history_rejects_wrong_registered_casino():
    async def handler(request):
        return httpx.Response(200, json={"status": "ready", "casino": "BluffPoker",
                                         "platform": "RunYourOwnPokerSite",
                                         "handHistoryUploadToAzure": True})
    http = httpx.AsyncClient(base_url="http://pdecision.test", transport=httpx.MockTransport(handler))
    api = PDecision(client=http)
    try:
        with pytest.raises(RuntimeError, match="destination must be casino Evenbet"):
            await api.require_evenbet_hand_history()
    finally:
        await api.close()


async def test_newcomer_buyin_during_decision_does_not_discard_or_retry(state):
    state["aa"] = [1, 2, 8, 15]
    state["s"][2]["b"] = 0
    state["m"]["r"] = 3
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = copy.deepcopy(state)
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class DelayedBet(FakeDecisions):
        async def decide(self, frame):
            self.frames.append(frame)
            c.states[state["ti"]]["s"][3] = {"i": 123, "n": "Kristiny", "c": 14823}
            return {"Action": 3, "BetSize": 10}
    decisions = DelayedBet()
    bot = Bot(c, decisions, state["ti"])
    await bot.process(state)
    await bot.process(c.states[state["ti"]])
    assert played == [(state["ti"], 8, 1000)]
    assert len(decisions.frames) == 1


async def test_captured_hand_162963359_sends_turn_bet_once():
    frames = json.loads((Path(__file__).parent / "fixtures/turn_newcomer.json").read_text())
    assert len(frames) == 3
    c = EvenBetClient()
    c.player_id = 73582719
    table_id = frames[0]["ti"]
    c.states[table_id] = frames[0]
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class CapturedDecision(FakeDecisions):
        async def decide(self, frame):
            self.frames.append(frame)
            c.states[table_id] = frames[-1]
            return {"Action": 3, "BetSize": 10}
    decisions = CapturedDecision()
    bot = Bot(c, decisions, table_id)
    for frame in frames:
        await bot.process(frame)
    assert played == [(table_id, 8, 1000)]
    assert len(decisions.frames) == 1
    hs = bot.mapper.convert(frames[-1], {})
    assert hs["EmptySeats"][1]
    assert hs["Names"][1] == "" and hs["Stacks"][1] == 0


async def test_strategy_exception_folds_and_keeps_bot_running(state):
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class FailedDecision(FakeDecisions):
        async def decide(self, frame):
            self.frames.append(frame)
            return {"Action": 6}
    bot = Bot(c, FailedDecision(), state["ti"])
    await bot.process(state)
    assert played == [(state["ti"], 1, 0)]
    assert bot.actions == 1


async def test_illegal_check_logs_error_and_folds(state, caplog):
    state["aa"] = [1, 3, 9, 15]
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class InvalidCheck(FakeDecisions):
        async def decide(self, frame):
            return {"Action": 2}
    await Bot(c, InvalidCheck(), state["ti"]).process(state)
    assert played == [(state["ti"], 1, 0)]
    assert "returned CHECK" in caplog.text and "folding" in caplog.text


async def test_hand_with_a_manual_turn_stays_manual_until_next_hand(state):
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = copy.deepcopy(state)
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class Control:
        manual = True
        async def offer(self, key): return self.manual, None
        async def claim_auto(self, key): return True
    control = Control()
    bot = Bot(c, FakeDecisions(), state["ti"], control=control)
    await bot.process(state)
    control.manual = False
    await bot.process(copy.deepcopy(state))
    assert not played
    next_hand = copy.deepcopy(state)
    next_hand["gi"] = state["gi"] + 1
    c.states[state["ti"]] = next_hand
    await bot.process(next_hand)
    assert played == [(state["ti"], 3, 100)]


async def test_unplayable_decision_takes_the_safe_action_and_keeps_the_bot_running(state):
    # A raise to less than the current bet cannot be played; the table worker must not stop on it.
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    played = []
    async def play(*args):
        played.append(args)
    c.play = play
    class BadRaise(FakeDecisions):
        async def decide(self, frame):
            self.frames.append(frame)
            return {"Action": 4, "BetSize": 0}
    bot = Bot(c, BadRaise(), state["ti"])
    await bot.process(state)
    assert played == [(state["ti"], 1, 0)]
    assert bot.actions == 1


async def test_bot_plays_when_the_manual_control_database_is_unreachable(state):
    from unittest.mock import AsyncMock
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    c.play = AsyncMock()
    control = type("Control", (), {"offer": AsyncMock(side_effect=OSError("database timeout")),
                                    "claim_auto": AsyncMock(side_effect=OSError("database timeout"))})()
    await Bot(c, FakeDecisions(), state["ti"], control=control).process(state)
    c.play.assert_awaited_once_with(state["ti"], 3, 100)
