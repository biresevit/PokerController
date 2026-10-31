"""The bot returns from an unintended sit-out, and the live view gets the acting seat's clock."""
from unittest.mock import AsyncMock

from poker_controller.bot import Bot
from poker_controller.client import EvenBetClient
from poker_controller.manual import clock, options

HERO = 2


class Decisions:
    async def decide(self, frame):
        return {"Action": 0}


def away(state):
    state["s"][HERO]["f"] |= 1
    state["m"]["ai"] = 0  # Not the hero's turn: a sitting-out hero is not dealt in.
    return state


def bot_for(state, control=None):
    c = EvenBetClient()
    c.player_id = 73582719
    c.states[state["ti"]] = state
    c.sit_back = AsyncMock()
    return Bot(c, Decisions(), state["ti"], control=control)


class Control:
    def __init__(self, manual):
        self.manual = manual

    async def manual_enabled(self):
        return self.manual

    async def offer(self, key):
        return False, None


async def test_bot_presses_im_back_after_being_sat_out(state):
    bot = bot_for(away(state))
    await bot.process(state)
    bot.client.sit_back.assert_awaited_once_with(state["ti"])
    await bot.process(state)  # Throttled: the table needs a moment to apply it.
    bot.client.sit_back.assert_awaited_once()


async def test_bot_stays_away_while_leaving_the_table(state):
    bot = bot_for(away(state))
    bot.leaving = True
    await bot.process(state)
    bot.client.sit_back.assert_not_awaited()


async def test_manual_control_leaves_im_back_to_the_human(state):
    bot = bot_for(away(state), Control(manual=True))
    await bot.process(state)
    bot.client.sit_back.assert_not_awaited()
    bot = bot_for(state, Control(manual=False))
    await bot.process(state)
    bot.client.sit_back.assert_awaited_once()


async def test_seated_hero_is_left_alone_and_a_pending_sit_out_clears_once_away(state):
    bot = bot_for(state)
    bot.sit_out_requested = True
    bot.decisions = type("D", (), {"decide": AsyncMock(side_effect=AssertionError("not needed"))})()
    state["m"]["ai"] = 0
    await bot.process(state)
    bot.client.sit_back.assert_not_awaited()
    assert bot.sit_out_requested
    await bot.process(away(state))
    assert not bot.sit_out_requested


def test_options_report_away_and_table_command_support(state):
    o = options(state, HERO)
    assert o["away"] is False and o["table_commands"] is True
    assert options(away(state), HERO)["away"] is True


def test_clock_counts_from_when_the_state_arrived(state):
    c = clock(state, received=100.0, now=103.5)
    assert c == {"seat": 2, "move_seconds": 24.019039, "bank_seconds": 0.0, "age": 3.5}
    state["t"].update(tr=0, tb=18)
    assert clock(state, 100.0, 100.0)["bank_seconds"] == 18.0
    state["t"] = {}
    assert clock(state, 100.0, 100.0) is None
    state["t"] = {"tr": 10}
    state["m"]["ai"] = 3  # Empty seat.
    assert clock(state, 100.0, 100.0) is None
    assert clock(state, None, 100.0) is None
