import copy

import pytest

from poker_controller.client import to_funds
from poker_controller.state import HandStateMapper, card, decision_to_play, turn_key
from poker_controller.transport import ProtocolError, ensure_success, redact


def test_card_mapping_and_hidden_cards():
    assert [card(n) for n in (0, 1, 32, 51, -1)] == ["2c", "2d", "tc", "as", ""]
    with pytest.raises(ValueError):
        card(52)


@pytest.mark.parametrize("amount,expected", [("80", 8000), ("0.01", 1), ("20.30", 2030)])
def test_money_is_exact(amount, expected):
    assert to_funds(amount) == expected


@pytest.mark.parametrize("amount", ["-1", "0.001", "nan", "inf"])
def test_invalid_money(amount):
    with pytest.raises(ValueError):
        to_funds(amount)


def test_real_snapshot_rotates_hero_and_defaults_omitted_zero_dealer(state):
    mapper = HandStateMapper(73582719, "monica")
    hs = mapper.convert(state, {"n": "Call Time 30min"})
    assert hs["OriginalNames"][0] == "monica"
    assert hs["Cards"][:2] == ["as", "7s"]
    assert hs["Stacks"][0] == 77
    assert hs["Bets"][0] == 1
    assert hs["DealerID"] == 3  # absolute dealer 0, hero 2, five seats
    assert hs["IsNewHand"] and hs["IsMyTurn"]
    assert not mapper.convert(state, {})["IsNewHand"]


def test_seat_zero_can_act_when_ai_omitted(state):
    state["m"].pop("ai")
    assert turn_key(state, 0)
    assert turn_key(state, 2) is None


def test_duplicate_turn_key_ignores_clocks_and_player_statistics(state):
    other = copy.deepcopy(state)
    other["t"]["tr"] = 7
    other["s"][0]["ct"] += 4
    assert turn_key(state, 2) == turn_key(other, 2)
    other["s"][0]["b"] += 100
    assert turn_key(state, 2) != turn_key(other, 2)


def test_raise_to_becomes_additional_funds(state):
    assert decision_to_play({"Action": 4, "BetSize": 6}, state, 2) == (9, 500)
    assert decision_to_play({"Action": 4, "BetSize": 2}, state, 2) == (9, 300)
    assert decision_to_play({"Action": 4, "BetSize": 9999}, state, 2) == (9, 7700)
    assert decision_to_play({"Action": 1}, state, 2) == (3, 100)
    assert decision_to_play({"Action": 0}, state, 2) == (1, 0)


def test_short_stack_call(state):
    state["s"][2]["c"] = 30
    assert decision_to_play({"Action": 1}, state, 2) == (3, 30)


@pytest.mark.parametrize("legal", [[1, 3], [1, 3, 15]])
@pytest.mark.parametrize("stack,expected", [(7700, 100), (30, 30)])
def test_raise_becomes_call_when_short_allin_does_not_reopen_betting(state, legal, stack, expected):
    state["aa"] = legal
    state["s"][2]["c"] = stack
    assert decision_to_play({"Action": 4, "BetSize": 50}, state, 2) == (3, expected)


@pytest.mark.parametrize("legal", [[1], [1, 2], [1, 3, 8]])
def test_raise_fallback_is_limited_to_call_fold_only(state, legal):
    state["aa"] = legal
    with pytest.raises(ValueError, match="not legal"):
        decision_to_play({"Action": 4, "BetSize": 50}, state, 2)


@pytest.mark.parametrize("legal,expected", [([1, 2, 15], (2, 0)), ([1, 3, 9], (1, 0))])
def test_strategy_exception_takes_only_free_exit(state, legal, expected):
    state["aa"] = legal
    assert decision_to_play({"Action": 6}, state, 2) == expected


def test_strategy_exception_without_check_or_fold_stops(state):
    state["aa"] = [3, 9]
    with pytest.raises(ValueError, match="no safe"):
        decision_to_play({"Action": 6}, state, 2)


def test_illegal_free_check_folds_instead_of_paying_to_call(state):
    state["aa"] = [1, 3, 9, 15]
    assert decision_to_play({"Action": 2}, state, 2) == (1, 0)


def test_illegal_or_malformed_decisions_fail_without_action(state):
    for decision in ({"Action": 9}, {"Action": 4, "BetSize": "nan"},
                     {"Action": "4"}, {"Action": 4, "BetSize": 0}):
        with pytest.raises(ValueError):
            decision_to_play(decision, state, 2)


def test_unknown_hero_and_hidden_cards_cannot_play(state):
    assert HandStateMapper(123, "absent").convert(state, {}) is None
    state["s"][2]["d"] = "-1;-1"
    assert HandStateMapper(73582719, "monica").convert(state, {}) is None


def test_reserved_seat_not_treated_as_hero(state):
    state["s"][2]["f"] |= 128
    assert HandStateMapper(73582719, "monica").convert(state, {}) is None


def test_protocol_errors_and_credentials_are_redacted():
    with pytest.raises(ProtocolError):
        ensure_success({"t": "LoginResult", "resultCode": 3, "resultCodeExt": 31})
    assert ensure_success({"t": "Result"}) == {"t": "Result"}
    assert redact({"auth": "secret", "nested": [{"password": "secret"}]}) == {
        "auth": "<redacted>", "nested": [{"password": "<redacted>"}]}


def test_newcomer_is_empty_until_next_hand(state):
    mapper = HandStateMapper(73582719, "monica")
    before = mapper.convert(state, {})
    state["s"][3] = {"i": 123, "n": "Newcomer", "c": 10000, "f": 32768}
    after = mapper.convert(state, {})
    for field in ("EmptySeats", "Names", "OriginalNames", "Stacks", "Bets", "InGame"):
        assert after[field] == before[field]
    assert after["EmptySeats"][1] and after["Names"][1] == ""
    assert after["Stacks"][1] == 0
    state["gi"] += 1
    state["s"][3]["d"] = "-1;-1"
    new_hand = mapper.convert(state, {})
    assert not new_hand["EmptySeats"][1]
    assert new_hand["Names"][1] == "Newcomer"
    assert new_hand["Stacks"][1] == 100


@pytest.mark.parametrize("replacement", [None, {"i": 123, "n": "Replacement", "c": 10000}])
def test_departed_player_keeps_identity_balance_and_current_street_bet(state, replacement):
    mapper = HandStateMapper(73582719, "monica")
    before = mapper.convert(state, {})
    state["s"][0] = replacement
    after = mapper.convert(state, {})
    for field in ("EmptySeats", "Names", "OriginalNames", "Stacks", "Bets"):
        assert after[field] == before[field]
    assert not after["InGame"][3]
    # The same seat stays attached to the old player, even after a replacement buys in.
    assert mapper.convert(state, {})["Stacks"][3] == before["Stacks"][3]
    state["m"]["r"] += 1
    assert mapper.convert(state, {})["Bets"][3] == 0


def test_sitout_preserves_roster_and_fold_updates_in_game(state):
    mapper = HandStateMapper(73582719, "monica")
    before = mapper.convert(state, {})
    key = mapper.turn_key(state, 2)
    state["s"][0]["f"] |= 1
    assert mapper.turn_key(state, 2) == key  # Away status alone is not a fold.
    state["s"][0].pop("d")
    after = mapper.convert(state, {})
    for field in ("EmptySeats", "Names", "Stacks", "Bets"):
        assert after[field] == before[field]
    assert not after["InGame"][3]
    assert mapper.turn_key(state, 2) != key


def test_call_with_nothing_to_call_is_the_free_check(state):
    # The big blind in an unraised tournament pot: PDecision says CALL, EvenBet offers fold/check/raise.
    blind = copy.deepcopy(state)
    blind["aa"] = [1, 2, 9, 15]
    assert decision_to_play({"Action": 1, "BetSize": 2}, blind, 2) == (2, 0)
