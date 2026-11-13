from poker_controller.metrics import decision_ms, opponents_in_hand


def test_pdecision_pause_is_not_counted_as_decision_time():
    # live answers, MSI 13 Nov 2026: 48 ms calculation and a 2.7 s pause inside the same call
    assert decision_ms({"DurationMS": 48, "RandomTimingMS": 2696, "TotalDuration": 2744}, 2760) == 64
    assert decision_ms({"DurationMS": 0, "RandomTimingMS": 7757}, 7770) == 13
    assert decision_ms({"Action": 3}, 120) == 120
    assert decision_ms({"RandomTimingMS": 900}, 500) == 0


def test_opponents_are_players_still_holding_cards():
    state = {"s": [{"d": "1;2"}, {"d": ""}, None, {"d": "-1;-1"}, {"d": "-1;-1"}]}
    assert opponents_in_hand(state, 0) == 2
