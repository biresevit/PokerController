"""Minimum human reaction time before a bot acts.

Events reach the bot from the game server sooner than a player's client could show them, so an action sent the
moment PDecision answers can be faster than any human: folding while the cards are still being dealt, raising
3x without the time to press the 3x and Raise buttons, calling a bet the instant it was made. The worker adds
this time after PDecision's answer, for every site (protocol bots like EvenBet and BluffPoker, and later UI bots).
"""
from __future__ import annotations

import random

# Seconds added after PDecision's answer.
RAISE_SECONDS = 3.0          # bet or raise: choose the size, then press Raise
PASSIVE_SECONDS = 1.0        # call, check or fold
FIRST_PREFLOP_SECONDS = 2.0  # first to act preflop: the cards are still being dealt on screen
# Each wait varies by up to this fraction, so it never repeats exactly.
JITTER = 0.3
# PDecision already took this long (its own human-like timing): nothing is added.
SKIP_AFTER_SECONDS = 8.0
# PDecision's time plus this wait stays within this, so the extra time never eats into the move timer or time bank.
MAX_TOTAL_SECONDS = 10.0

RAISE_KINDS = {3, 4}  # PDecision Action: 0 fold, 1 call, 2 check, 3 bet, 4 raise, 6 no strategy (check/fold)


def first_to_act_preflop(hand_state: dict) -> bool:
    """No board yet, nobody folded, and only the blinds have put money in."""
    cards = hand_state.get("Cards") or []
    if len(cards) > 2 and any(cards[2:]):
        return False
    dealt = [not empty for empty in hand_state.get("EmptySeats") or []]
    in_game = hand_state.get("InGame") or []
    if any(d and not g for d, g in zip(dealt, in_game)):
        return False
    return sum(1 for bet in hand_state.get("Bets") or [] if bet and bet > 0) <= 2


def extra_seconds(hand_state: dict, kind, decided_seconds: float, rng=random) -> float:
    """Seconds to wait before sending the action PDecision chose (`kind`) after it took `decided_seconds`."""
    if decided_seconds >= SKIP_AFTER_SECONDS:
        return 0.0
    base = RAISE_SECONDS if kind in RAISE_KINDS else PASSIVE_SECONDS
    if first_to_act_preflop(hand_state):
        base += FIRST_PREFLOP_SECONDS
    wait = base * (1 + rng.uniform(0, JITTER))
    return max(0.0, min(wait, MAX_TOTAL_SECONDS - decided_seconds))
