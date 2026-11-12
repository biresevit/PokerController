"""Translate EvenBet snapshots into PDecision's hero-relative HandState contract."""
from __future__ import annotations

import copy
import hashlib
import json
from decimal import Decimal, ROUND_HALF_UP


def amount_scale(table: dict) -> int:
    """Protocol units per HandState unit: cash cents, or native tournament chips."""
    return 1 if (table.get('tournament') or {}).get('format') in ('SNG', 'MTT') else 100


def card(code: int | str) -> str:
    code = int(code)
    if code == -1:
        return ""
    if not 0 <= code < 52:
        raise ValueError(f"Unsupported card index {code}")
    return "23456789tjqka"[code // 4] + "cdhs"[code % 4]


def cards(raw: str | None) -> list[str]:
    return [card(c) for c in raw.split(";") if c != ""] if raw else []


def turn_key(state: dict, hero: int) -> str | None:
    if state.get("m", {}).get("ai", 0) != hero or not set(state.get("aa", [])) & {1, 2, 3, 8, 9}:
        return None
    # Excludes clocks/message IDs: redundant snapshots must not produce duplicate actions.
    relevant = {k: state.get(k) for k in ("ti", "gi", "d", "f", "aa")}
    relevant["m"] = {k: state.get("m", {}).get(k, 0) for k in ("ai", "di", "r")}
    relevant["s"] = [{k: s.get(k, 0) for k in ("i", "c", "b", "d")} if s else None
                     for s in state.get("s", [])]
    return hashlib.sha256(json.dumps(relevant, sort_keys=True).encode()).hexdigest()


class HandStateMapper:
    def __init__(self, player_id: int, username: str, platform: int = 11):
        self.player_id, self.username, self.platform = player_id, username, platform
        self.hand_ids: dict[int, int] = {}
        self.dealt: dict[int, set[int]] = {}
        self.hole_cards: dict[tuple[int, int], list[str]] = {}
        self.rosters: dict[int, list[dict | None]] = {}
        self.rounds: dict[int, int] = {}
        self.payout_bases: dict[tuple[int, int], list[int]] = {}

    def hand_snapshot(self, state: dict, *, update: bool = False) -> dict:
        """Project live seats onto the players dealt into this hand.

        Previewing a newer snapshot must not advance the mapper past queued events.
        Missing/replaced players retain their last balances until the street ends.
        """
        table_id = state["ti"]
        if self.hand_ids.get(table_id) != state.get("gi"):
            return state
        roster = copy.deepcopy(self.rosters[table_id])
        street = state.get("m", {}).get("r", 0)
        for i, old in enumerate(roster):
            if old is None:
                continue
            live = state["s"][i]
            if live and live.get("i") == old.get("i") and not live.get("f", 0) & 128:
                # Preserve identity; cash/bets/cards describe the current hand.
                for field in ("c", "b", "d"):
                    old[field] = live.get(field, "" if field == "d" else 0)
            else:
                old["d"] = ""
                if self.rounds[table_id] != street:
                    old["b"] = 0  # Previous street's contribution is now in the pot.
        if update:
            self.rosters[table_id] = roster
            self.rounds[table_id] = street
        return {**state, "s": roster}

    def turn_key(self, state: dict, hero: int) -> str | None:
        return turn_key(self.hand_snapshot(state), hero)

    def convert(self, state: dict, table: dict) -> dict | None:
        seats = state.get("s", [])
        hero = next((i for i, s in enumerate(seats) if s and s.get("i") == self.player_id
                     and not s.get("f", 0) & 128), None)
        if hero is None or not state.get("gi"):
            return None
        game = state.get("gt", table.get("g"))
        hole_count = {72: 2, 79: 4, 88: 5}.get(game)
        if hole_count is None:
            raise ValueError(f"Unsupported game type {game}; NLH, PLO4 and PLO5 are supported")
        attrs = table.get("attrs", {})
        if str(attrs.get("doubleBoard", "0")) == "1" or str(attrs.get("isBombPot", "0")) == "1":
            raise ValueError("Double-board and bomb-pot hands require a different decision contract")
        hole = cards(seats[hero].get("d"))
        hand_key = (state["ti"], state["gi"])
        if len(hole) >= hole_count and all(hole[:hole_count]):
            self.hole_cards[hand_key] = hole[:hole_count]
        elif hand_key in self.hole_cards:
            hole = self.hole_cards[hand_key]
        # Never send an observer's or mid-hand attachment's unknown hole cards as a playable hand.
        if len(hole) < hole_count or not all(hole[:hole_count]):
            return None
        table_id, hand_id = state["ti"], state["gi"]
        is_new = self.hand_ids.get(table_id) != hand_id
        if is_new:
            self.hand_ids[table_id] = hand_id
            self.dealt[table_id] = {i for i, seat in enumerate(seats) if seat and seat.get("d")}
            self.rosters[table_id] = [copy.deepcopy(s) if i in self.dealt[table_id] else None
                                      for i, s in enumerate(seats)]
            self.rounds[table_id] = state.get("m", {}).get("r", 0)
            self.hole_cards = {k: v for k, v in self.hole_cards.items() if k[0] != table_id or k[1] == hand_id}
            self.payout_bases = {k: v for k, v in self.payout_bases.items() if k[0] != table_id or k[1] == hand_id}
        seats = self.hand_snapshot(state, update=True)["s"]
        n = len(seats)
        order = [(hero + i) % n for i in range(n)]
        rotated = [seats[i] for i in order]
        board = cards(state.get("d", {}).get("c"))
        if len(board) > 5:
            raise ValueError("Multiple boards are unsupported")
        scale = amount_scale(table)
        def amount(s, key):
            return (s or {}).get(key, 0) / scale
        def name(s):
            return (s.get("dn") or s.get("n", "")) if s and not s.get("f", 0) & 128 else ""
        names = [name(s) for s in rotated]
        names[0] = self.username  # PDecision keys profiles and per-table state by the account name.
        showdowns = ["" if i == 0 else "".join(cards(s.get("d"))) if s else ""
                     for i, s in enumerate(rotated)]
        dealer = state.get("m", {}).get("di", 0)
        if dealer >= n:
            return None
        if state.get("ts") == 1:
            self.payout_bases[hand_key] = [(s or {}).get("c", 0) for s in state.get("s", [])]
        winnings = [0] * n
        if state.get("ts") == 3 and hand_key in self.payout_bases:
            base = self.payout_bases[hand_key]
            winnings = [max(0, (state["s"][i] or {}).get("c", 0) - base[i]) / scale
                        for i in order]
        # Tournament tables carry their format and standings (set by the worker from tournament messages):
        # PDecision's tournament strategy needs players left/total and paid places. Ring tables: TableType 1.
        tournament = table.get("tournament") or {}
        table_type = {"SNG": 2, "MTT": 3}.get(tournament.get("format"), 1)
        ante = tournament.get("ante")
        request = {
            "TableID": str(table_id), "TableName": table.get("n", str(table_id)), "TableHandle": 0,
            "HandID": str(hand_id), "IsNewHand": is_new,
            "IsMyTurn": turn_key(state, hero) is not None,
            "Platform": self.platform, "GameType": {72: 2, 79: 4, 88: 5}[game], "TableType": table_type,
            "BBSize": state.get("bbv", table.get("bb", 0)) / scale,
            "SBSize": state.get("sbv", table.get("s", 0)) / scale,
            "Ante": (ante if ante is not None else table.get("an", 0)) / max(state.get("bbv", 1), 1), "Straddle": 0,
            "DealerID": (dealer - hero) % n, "Names": names, "OriginalNames": list(names),
            "EmptySeats": [i not in self.dealt[table_id] for i in order],
            "InGame": [bool(s and s.get("d")) for s in rotated],
            "Stacks": [amount(s, "c") for s in rotated], "Bets": [amount(s, "b") for s in rotated],
            "Pot": state.get("d", {}).get("p", 0) / scale,
            "Cards": hole[:2] + board + [""] * (5 - len(board)),
            "OmahaCards": hole[:hole_count] if hole_count > 2 else [], "Showdowns": showdowns,
            "Winners": winnings, "IsWinners": [w > 0 for w in winnings],
            "PlayersLeft": int(tournament.get("players_left") or 0), "PlayersTotal": int(tournament.get("players_total") or 0),
            "NumPlacesPaid": int(tournament.get("places_paid") or 0),
        }
        # The tournament's buy-in (table currency, from the lobby): PDecision's minimum buy-in of card sharing and the
        # solver strategy. Not part of PDecision's HandState, so only sent when known.
        buy_in = tournament.get("buy_in")
        if buy_in:
            request["TournamentBuyIn"] = float(buy_in)
        return request


def complete_final_frame(previous: dict | None, final: dict) -> dict:
    """The hand-history frame of a hand won without a showdown, as PDecision's builder needs it.

    BluffPoker ends such a hand with an idle state: no board and no player dealt in, only the payout in the
    stacks. The builder takes the street from the board and the folds from who left the hand, so it wrote the
    fold to a river bet preflop (BluffPoker hand 1cc78d1e-...-9, 2026-11-12). The completed frame keeps the
    hand's board and the winners in the hand. EvenBet's last frame already has both and is returned unchanged.
    """
    if not previous or any(final.get("InGame", [])) or not any(w > 0 for w in final.get("Winners", [])):
        return final
    frame = copy.deepcopy(final)
    board, last_board = frame["Cards"][2:], previous["Cards"][2:]
    if sum(1 for c in board if c) < sum(1 for c in last_board if c):
        frame["Cards"] = frame["Cards"][:2] + list(last_board)
    frame["InGame"] = [bool(was) and won > 0 for was, won in zip(previous["InGame"], frame["Winners"])]
    return frame


def decision_to_play(decision: dict, state: dict, hero: int, *, scale: int = 100) -> tuple[int, int]:
    """Convert a raise-to amount to additional protocol units using the input hand's scale."""
    kind = decision.get("Action")
    if kind == 6:  # PDecision could not produce a strategy action.
        legal = state.get("aa", [])
        if 2 in legal:
            return 2, 0
        if 1 in legal:
            return 1, 0
        raise ValueError(f"PDecision Action=6 with no safe check/fold action: {legal}")
    if type(kind) is not int or kind not in range(5):
        raise ValueError(f"Unknown decision Action={kind!r}")
    legal = state.get("aa", [])
    # PDecision occasionally assumes a free check on special tables when the
    # server still requires a call. Never turn that check into a paid action.
    if kind == 2 and 2 not in legal and 1 in legal:
        return 1, 0
    # And the reverse: a CALL when there is nothing to call (e.g. the big blind in an unraised
    # tournament pot) is the free check, never an illegal call.
    if kind == 1 and 3 not in legal and 2 in legal:
        return 2, 0
    # A short all-in may not reopen betting. Ignore ancillary commands (e.g. 15).
    if kind == 4 and set(legal) & {1, 2, 3, 8, 9} == {1, 3}:
        kind = 1
    action = {0: 1, 1: 3, 2: 2, 3: 8, 4: 9}[kind]
    if action not in legal:
        raise ValueError(f"Decision action {action} is not legal: {legal}")
    if kind in (0, 2):
        return action, 0
    funds = state.get("f", {})
    stack = state["s"][hero].get("c", 0)
    if kind == 1:
        return action, min(funds.get("c", 0), stack)
    target = Decimal(str(decision["BetSize"])) * scale
    if not target.is_finite():
        raise ValueError("Non-finite bet size")
    additional = int(target.quantize(Decimal("1"), rounding=ROUND_HALF_UP)) - state["s"][hero].get("b", 0)
    minimum, maximum = funds.get("mn", 0), min(funds.get("mx", stack), stack)
    if maximum <= 0 or additional <= 0:
        raise ValueError("Invalid bet size or betting bounds")
    return action, max(min(minimum, maximum), min(additional, maximum))
