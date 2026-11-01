import pytest

from poker_controller.handrank import (FLUSH, FULL_HOUSE, HIGH_CARD, PAIR, QUADS, STRAIGHT, STRAIGHT_FLUSH, TRIPS,
                                       TWO_PAIR, best, category, show_reason)


@pytest.mark.parametrize("cards,expected", [
    ("as ks qs js ts", STRAIGHT_FLUSH), ("ad ac ah as 2c", QUADS), ("kd kc kh 2s 2c", FULL_HOUSE),
    ("2h 7h 9h jh kh", FLUSH), ("ad 2c 3h 4s 5c", STRAIGHT), ("9d tc jh qs kc", STRAIGHT),
    ("qd qc qh 2s 7c", TRIPS), ("qd qc 7h 7s 2c", TWO_PAIR), ("qd qc 7h 8s 2c", PAIR), ("qd jc 7h 8s 2c", HIGH_CARD),
    ("kd ac 2h 3s 4c", HIGH_CARD),  # No wrap-around straight.
    ("qd qc 7h", PAIR), ("qd qc qh 7s", TRIPS),
])
def test_categories_match_decisionmaking_hand_types(cards, expected):
    assert category(cards.split()) == expected


def test_omaha_must_use_exactly_two_hole_cards():
    # Four hearts on board and one heart in hand is no flush in Omaha, but is in hold'em.
    hole, board = ["ah", "kc", "2d", "3s"], ["4h", "7h", "9h", "jh", "qc"]
    assert best(hole[:2], board, omaha=False) == FLUSH
    assert best(hole, board, omaha=True) < FLUSH


@pytest.mark.parametrize("game,hole,board,expected", [
    (72, "qd qc", "qh 7s 2c", "strong"),            # set
    (72, "qd 7c", "qh 7s 2c", "strong"),            # two pair made with both hole cards
    (72, "ad kc", "7h 7s 2c 2d", None),             # two pair is the board's: not the hero's hand
    (72, "ad 7c", "7h 2s 2c", "strong"),            # board pair plus own pair
    (72, "ad kc", "7h 8s 2c", "bluff"),             # nothing
    (72, "ad kc", "7h 7s 2c", "bluff"),             # only the board's pair
    (72, "ad 8c", "7h 8s 2c", None),                # an honest one pair: neither strong nor a bluff
    (72, "ad kc", "", None),                        # preflop wins never qualify
    (79, "qd qc 5h 6s", "qh 7s 2c", "strong"),      # Omaha set
    (79, "qd 7c 5h 6s", "qh 7s 2c", None),          # Omaha two pair is not enough
    (79, "ad kc 5h 6s", "7h 9s 2c", "bluff"),
    (79, "ad kc 5h 4s", "3h 6s 2c 9d", "strong"),   # 2-6 straight with exactly 5h 4s
])
def test_show_reason(game, hole, board, expected):
    assert show_reason(game, hole.split(), board.split()) == expected
