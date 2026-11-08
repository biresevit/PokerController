import random

from poker_controller import pacing


def frame(board=(), bets=(0.25, 0.5, 0, 0, 0, 0), in_game=(True,) * 6):
    return {"Cards": ["ah", "kd", *board] + [""] * (5 - len(board)), "Bets": list(bets),
            "InGame": list(in_game), "EmptySeats": [False] * 6}


class Fixed:
    def uniform(self, a, b):
        return 0.0


def test_first_to_act_preflop_waits_for_the_deal():
    assert pacing.first_to_act_preflop(frame())
    assert not pacing.first_to_act_preflop(frame(in_game=(True, True, False, True, True, True)))  # someone folded
    assert not pacing.first_to_act_preflop(frame(bets=(0.25, 0.5, 0.5, 0, 0, 0)))  # someone limped
    assert not pacing.first_to_act_preflop(frame(board=("2c", "3d", "4h"), bets=(0,) * 6))


def test_extra_time_by_action():
    first, later = frame(), frame(board=("2c", "3d", "4h"), bets=(0, 1, 0, 0, 0, 0))
    assert pacing.extra_seconds(later, 4, 0.2, Fixed()) == 3.0   # raise
    assert pacing.extra_seconds(later, 1, 0.2, Fixed()) == 1.0   # call
    assert pacing.extra_seconds(later, 0, 0.2, Fixed()) == 1.0   # fold
    assert pacing.extra_seconds(first, 0, 0.2, Fixed()) == 3.0   # first to act folds: deal + fold
    assert pacing.extra_seconds(first, 4, 0.2, Fixed()) == 5.0   # first to act raises


def test_slow_pdecision_answer_is_not_delayed_further():
    later = frame(board=("2c", "3d", "4h"))
    assert pacing.extra_seconds(later, 4, 8.5, Fixed()) == 0.0
    assert pacing.extra_seconds(frame(), 4, 7.0, Fixed()) == 3.0  # capped at 10 s in total
    assert pacing.extra_seconds(later, 1, 5.0, Fixed()) == 1.0     # a 5 s answer still gets its minimum


def test_waits_vary():
    waits = {round(pacing.extra_seconds(frame(), 1, 0.1, random.Random(s)), 3) for s in range(5)}
    assert len(waits) > 1 and all(3.0 <= w <= 3.9 for w in waits)
