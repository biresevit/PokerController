"""Made-hand categories for deciding whether to show cards after an uncontested win.

Categories follow DecisionMaking's HandEvaluator.HandTypes: HighCard=0, Pair, TwoPair, Trips,
Straight, Flush, FullHouse, FourOfAKind, StraightFlush=8. Omaha hands use exactly two hole
cards and three board cards.
"""
from __future__ import annotations

from collections import Counter
from itertools import combinations

HIGH_CARD, PAIR, TWO_PAIR, TRIPS, STRAIGHT, FLUSH, FULL_HOUSE, QUADS, STRAIGHT_FLUSH = range(9)
RANKS = "23456789tjqka"


def category(cards: list[str]) -> int:
    """Category of up to five cards ("as", "td", ...). Straights and flushes need five cards."""
    ranks = sorted((RANKS.index(c[0]) for c in cards), reverse=True)
    counts = sorted(Counter(ranks).values(), reverse=True)
    if len(cards) == 5:
        flush = len({c[1] for c in cards}) == 1
        unique = sorted(set(ranks))
        straight = len(unique) == 5 and (unique[-1] - unique[0] == 4 or unique == [0, 1, 2, 3, 12])
        if straight and flush:
            return STRAIGHT_FLUSH
    else:
        flush = straight = False
    if counts[0] == 4:
        return QUADS
    if counts[:2] == [3, 2]:
        return FULL_HOUSE
    if flush:
        return FLUSH
    if straight:
        return STRAIGHT
    if counts[0] == 3:
        return TRIPS
    if counts[:2] == [2, 2]:
        return TWO_PAIR
    return PAIR if counts[0] == 2 else HIGH_CARD


def best(hole: list[str], board: list[str], omaha: bool) -> int:
    if omaha:
        return max(category(list(h) + list(b)) for h in combinations(hole, 2) for b in combinations(board, 3))
    cards = hole + board
    return max(category(list(c)) for c in combinations(cards, min(5, len(cards))))


def show_reason(game: int | None, hole: list[str], board: list[str]) -> str | None:
    """Why an uncontested winner might show: 'strong', 'bluff', or None when it should not.

    Strong: two pair or better in NLH that the hole cards make (not the board alone); three of a
    kind or better in Omaha. Bluff: the hole cards add nothing to the board (no pair of the
    hero's own). Preflop wins never qualify: there is no made hand to show.
    """
    hole = [c for c in hole if c]
    board = [c for c in board if c]
    if len(board) < 3 or len(hole) < 2:
        return None
    omaha = game in (79, 88) or len(hole) > 2
    hero = best(hole, board, omaha)
    on_board = category(board)
    if omaha and hero >= TRIPS or not omaha and hero >= TWO_PAIR and hero > on_board:
        return "strong"
    if hero == HIGH_CARD or hero == PAIR and on_board >= PAIR:
        return "bluff"
    return None
