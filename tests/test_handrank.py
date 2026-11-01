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


@pytest.mark.parametrize("game,hole,board,hand", [
    (72, "9d 8c", "7h 6s 5c", "straight"),
    (72, "ah 2h", "9h 7h 4h", "flush"),
    (72, "qd qc", "qh 7s 7c", "full house"),
    (72, "7d 7c", "7h 7s 2c", "quads"),
    (72, "9h 8h", "7h 6h 5h", "straight flush"),
    (79, "9d 8c 2s 2d", "7h 6s 5c", "straight"),
    (79, "ah 2h ks qd", "9h 7h 4h", "flush"),
    (79, "qd qc 3s 4d", "qh 7s 7c", "full house"),
    (79, "7d 7c 3s 4d", "7h 7s 2c", "quads"),
    (79, "9h 8h 2c 3d", "7h 6h 5h", "straight flush"),
])
def test_straights_flushes_full_houses_and_quads_are_strong(game, hole, board, hand):
    assert show_reason(game, hole.split(), board.split()) == "strong"


@pytest.mark.parametrize("hole,board", [
    ("2c 3d", "ah kh qh jh 9h"),   # the flush is entirely on the board
    ("2c 3d", "9d tc jh qs kc"),   # so is the straight
    ("2c 3d", "kd kc kh 7s 7c"),   # and the full house
])
def test_nlh_hand_made_only_by_the_board_is_not_strong(hole, board):
    assert show_reason(72, hole.split(), board.split()) != "strong"
