import copy
import json
from pathlib import Path

import httpx
import pytest

from poker_controller import bluffpoker as bp
from poker_controller.bot import Bot
from poker_controller.decision import PDecision
from poker_controller.sites import BLUFFPOKER, EVENBET, platform_id, table_key
from poker_controller.state import HandStateMapper, cards, decision_to_play

DATA = json.loads((Path(__file__).parent / "fixtures/bluffpoker.json").read_text())
PID = DATA["player_id"]
TABLE = "3b355673-ab0e-44f8-92cc-88021c2b03b8"


@pytest.fixture(autouse=True)
def fresh_credentials():
    bp.CREDENTIALS.clear()
    yield
    bp.CREDENTIALS.clear()


def settings(**changes):
    values = dict(hub_url="https://hub.test", api_key="key", origin="https://site.test", username="guido66",
                  email="guido66@bots.test", password="secret")
    values.update(changes)
    return bp.BluffPokerSettings(**values)


def state(name):
    return bp.game_state(copy.deepcopy(DATA["states"][name]), PID, key=TABLE)


class FakeSocket:
    def __init__(self, reply=None):
        self.sent = []
        self.reply = reply or {"type": "actionResult", "ok": True}
        self.closed = False
        self.wire = TABLE

    async def action(self, payload, timeout=None):
        self.sent.append(payload)
        return self.reply


def client_at(name):
    c = bp.BluffPokerClient(settings())
    c.player_id = PID
    c.states[TABLE] = state(name)
    c.tables[TABLE] = {"i": TABLE, "wire": TABLE, "g": 72, "p": 80, "bb": 100, "s": 50, "mn": 8000, "mx": 20000}
    c.sockets[TABLE] = FakeSocket()
    return c


def test_preflop_turn_is_evenbet_shaped():
    s = state("preflop_turn")
    assert s["ti"] == TABLE and s["gi"].startswith(TABLE + "-") and s["gt"] == 72
    assert s["m"] == {"ai": 0, "di": 0, "r": 1} and s["ts"] == 1
    hero = s["s"][0]
    assert hero["i"] == PID and cards(hero["d"]) == ["tc", "9d"] and hero["c"] == 17400 and hero["b"] == 50
    assert s["s"][1]["d"] == "-1;-1"  # the opponent is in the hand with hidden cards
    assert s["aa"] == [1, 3, 9]
    # call 0.50 more; raise to 2 (1.50 more) up to all-in 174.50 (174.00 more)
    assert s["f"] == {"c": 50, "mn": 150, "mx": 17400}
    assert s["bbv"] == 100 and s["sbv"] == 50
    assert s["d"]["p"] == 0  # the engine's pot of 1.50 is the blinds still in front of the players


def test_postflop_without_a_bet_offers_a_bet():
    s = state("postflop_bet")
    assert s["m"]["r"] == 2 and s["aa"] == [1, 2, 8]
    assert s["f"]["mn"] == 100 and s["d"]["p"] == 400 and len(cards(s["d"]["c"])) == 3


def test_first_idle_state_after_a_hand_finishes_it():
    s = state("hand_over")
    assert s["ts"] == 3 and s["aa"] == [] and s["m"]["ai"] == -1


def test_unsupported_formats_are_refused():
    raw = copy.deepcopy(DATA["states"]["preflop_turn"])
    raw["doubleBoard"] = True
    with pytest.raises(bp.BluffPokerError) as error:
        bp.game_state(raw, PID, key=TABLE)
    assert error.value.code == "UNSUPPORTED_FORMAT_doubleBoard" and error.value.terminal


def test_tournament_amounts_are_whole_chips():
    raw = copy.deepcopy(DATA["states"]["preflop_turn"])
    raw["tournamentMeta"] = {"ante": 0}
    for seat in raw["seats"]:
        if not seat.get("empty"):
            seat["stack"], seat["betThisRound"] = 1500, 20
    raw.update(bigBlind=20, smallBlind=10, streetMaxBet=20, minRaiseTotal=40, pot=40)
    s = bp.game_state(raw, PID, key=TABLE)
    assert s["s"][0]["c"] == 1500 and s["bbv"] == 20 and s["bp"]["unit"] == 100


def test_handstate_for_pdecision():
    hs = HandStateMapper(PID, "guido66", BLUFFPOKER).convert(state("preflop_turn"), {"n": "NLH 0,50/1"})
    assert hs["Platform"] == 18 and hs["TableType"] == 1 and hs["IsMyTurn"]
    assert hs["BBSize"] == 1 and hs["SBSize"] == 0.5 and hs["Bets"] == [0.5, 1] and hs["Pot"] == 0
    assert hs["Names"][0] == "guido66" and hs["Cards"][:2] == ["tc", "9d"]


@pytest.mark.parametrize("action,funds,sent", [
    (1, 0, {"action": "fold"}),
    (3, 50, {"action": "call"}),
    (9, 150, {"action": "raise", "amount": 2.0}),        # raise to 2: the street total
    (9, 17400, {"action": "allin"}),                      # the whole stack
])
async def test_evenbet_actions_become_bluffpoker_actions(action, funds, sent):
    c = client_at("preflop_turn")
    assert await c.play(TABLE, action, funds) == {"type": "actionResult", "ok": True}
    assert c.sockets[TABLE].sent == [{"type": "action", "tableId": TABLE, **sent}]


async def test_bet_is_a_raise_to_the_street_total():
    c = client_at("postflop_bet")
    await c.play(TABLE, 8, 250)
    assert c.sockets[TABLE].sent[0] == {"type": "action", "tableId": TABLE, "action": "raise", "amount": 2.5}


async def test_refused_action_raises_and_illegal_action_is_never_sent():
    c = client_at("preflop_turn")
    c.sockets[TABLE].reply = {"type": "actionResult", "ok": False, "error": "NOT_YOUR_TURN"}
    with pytest.raises(bp.BluffPokerError, match="NOT_YOUR_TURN"):
        await c.play(TABLE, 3, 50)
    with pytest.raises(ValueError):
        await c.play(TABLE, 2, 0)  # check is not legal facing the big blind
    assert len(c.sockets[TABLE].sent) == 1


async def test_bot_plays_pdecision_raise_on_bluffpoker():
    c = client_at("preflop_turn")
    played = []

    async def play(*args):
        played.append(args)
    c.play = play

    class Decisions:
        async def decide(self, hs):
            self.hs = hs
            return {"Action": 4, "BetSize": 3}
    decisions = Decisions()
    await Bot(c, decisions, TABLE).process(c.states[TABLE])
    assert decisions.hs["Platform"] == 18
    assert played == [(TABLE, 9, 250)]  # raise to 3.00 from 0.50 in front
    assert decision_to_play({"Action": 1}, c.states[TABLE], 0) == (3, 50)


def test_lobby_keeps_plain_public_tables_only():
    rows = [bp.lobby_row(r) for r in DATA["lobby"]["tables"]]
    assert rows[1] is None and rows[2] is None  # double board, private
    assert rows[0] == {"i": "6ab84bec-ddf7-4f60-a11a-9502d1e13ef0", "wire": "6ab84bec-ddf7-4f60-a11a-9502d1e13ef0",
                       "n": "NLH 0,50/1", "g": 72, "p": 80, "bb": 100, "s": 50, "mp": 6, "np": 0,
                       "mn": 8000, "mx": 20000}


def test_rejoin_minimum_is_read_from_the_refusal():
    assert bp.rejoin_minimum(DATA["rejoin_refusal"]) == 17450
    assert bp.rejoin_minimum({"error": "REJOIN_MIN_NOT_MET"}) is None


async def test_rejoin_buys_in_exactly_the_stack_left_with():
    c = client_at("hand_over")
    hero = c.states[TABLE]["s"][0]
    c.states[TABLE]["s"][0] = None  # not seated yet
    calls = []

    class Hub:
        async def buy_in_seat(self, table, seat, cents, key):
            calls.append((seat, cents))
            if len(calls) == 1:
                raise bp.BluffPokerError("REJOIN_MIN_NOT_MET", status=409, terminal=True, body=DATA["rejoin_refusal"])
            c.states[TABLE]["s"][0] = hero
            return {"ok": True}
    c.hub = Hub()
    assert await c.sit(TABLE, "100") == 0
    assert calls == [(0, 10000), (0, 17450)]


def test_card_index_matches_evenbet():
    assert cards(bp.card_list(["2c", "Ah", "Td", "Ks"])) == ["2c", "ah", "td", "ks"]


def test_float_cap_never_exceeds_the_engine_value():
    assert bp.cap_to_cents(24.089999999999996) == 2408
    assert bp.cap_to_cents(24.09) == 2409 and bp.to_cents("1.5") == 150 and bp.to_cents(1.234) is None


def hub_with(handler, **changes):
    s = settings(**changes)
    return bp.Hub(s, http=httpx.AsyncClient(base_url=s.hub_url, transport=httpx.MockTransport(handler)))


async def test_login_refreshes_once_on_401_and_builds_table_url():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization"),
                     request.headers.get("x-api-key"), request.headers.get("origin")))
        path = request.url.path
        if path.endswith("/auth/login"):
            assert json.loads(request.content) == {"email": "guido66@bots.test", "password": "secret"}
            return httpx.Response(200, json={"pokerToken": "a1", "refreshToken": "r1", "expiresIn": 3600,
                                             "clientUserId": PID})
        if path == "/auth/refresh":
            return httpx.Response(200, json={"accessToken": "a2", "refreshToken": "r2", "expiresIn": 3600})
        if path.endswith("/connection"):
            return httpx.Response(200, json={"wsUrl": "wss://engine.test/ws/8030", "tableId": TABLE,
                                             "gameType": "NLH", "engineAuthMode": "jwt"})
        if path.endswith("/ws-token"):
            if request.headers["authorization"] == "Bearer a1":
                return httpx.Response(401, json={"error": "UNAUTHORIZED"})
            return httpx.Response(200, json={"wsToken": "table-jwt", "expiresIn": 60})
        return httpx.Response(404)

    hub = hub_with(handler)
    await hub.login()
    assert hub.player_id == PID
    url = await hub.endpoint(TABLE, "NLH")
    assert url == "wss://engine.test/ws/8030?token=table-jwt"
    assert [s[1] for s in seen] == ["/integrations/clients/auth/login", f"/integrations/clients/tables/{TABLE}/connection",
                                    f"/integrations/clients/tables/{TABLE}/ws-token", "/auth/refresh",
                                    f"/integrations/clients/tables/{TABLE}/ws-token"]
    assert all(s[3] == "key" and s[4] == "https://site.test" for s in seen)
    await hub.close()


async def test_static_engine_credential_is_refused_unless_allowed():
    def handler(request):
        return httpx.Response(200, json={"wsUrl": "wss://engine.test/ws", "tableId": TABLE,
                                         "engineAuthMode": "static", "engineThumbKey": "thumb"})
    hub = hub_with(handler)
    with pytest.raises(bp.BluffPokerError, match="STATIC_CREDENTIAL_NOT_ALLOWED"):
        await hub.endpoint(TABLE)
    allowed = hub_with(handler, allow_static_credential=True)
    assert await allowed.endpoint(TABLE) == "wss://engine.test/ws?thumbKey=thumb"


async def test_unconfirmed_buy_in_is_asked_again_with_the_same_key():
    keys = []

    def handler(request):
        keys.append(request.headers["x-idempotency-key"])
        if len(keys) == 1:
            return httpx.Response(503, json={"error": "UPSTREAM"})
        return httpx.Response(200, json={"ok": True, "sessionId": "s", "walletId": "w", "reservedChips": 100,
                                         "engine": {"ok": True, "seat": 1}, "duplicate": False})
    c = bp.BluffPokerClient(settings(), hub=hub_with(handler))
    c.hub.access, c.hub.expires = "a1", c.hub.clock() + 3600
    await c.money(c.hub.buy_in_seat, TABLE, 1, 10000, "key-1")
    assert keys == ["key-1", "key-1"]


def test_settings_come_from_the_config_file_with_environment_overrides(tmp_path):
    path = tmp_path / "bluffpoker.json"
    path.write_text(json.dumps({"hub_url": "https://hub.test/", "api_key": "file-key", "origin": "https://site.test",
                                "accounts": {"Guido66": {"email": "g@test", "password": "pw"}}}))
    s = bp.BluffPokerSettings.load("guido66", path=path, environ={"BP_CLIENT_API_KEY": "env-key"})
    assert (s.hub_url, s.api_key, s.email, s.password) == ("https://hub.test", "env-key", "g@test", "pw")
    assert "pw" not in repr(s) and "env-key" not in repr(s)
    with pytest.raises(ValueError, match="api_key"):
        bp.BluffPokerSettings.load("guido66", path=tmp_path / "missing.json", environ={})
    # the login stored with the account in the database wins over the file's; accounts need no file entry then
    s = bp.BluffPokerSettings.load("guido66", path=path, environ={}, email="db@test", password="db-pw")
    assert (s.email, s.password) == ("db@test", "db-pw")
    s = bp.BluffPokerSettings.load("newbot", path=path, environ={}, email="new@test", password="new-pw")
    assert (s.email, s.password) == ("new@test", "new-pw")
    assert bp.BluffPokerSettings.load("guido66", path=path, environ={}, email=None, password=None).email == "g@test"


def test_platforms_and_table_keys():
    assert platform_id("BluffPokerPlatform") == BLUFFPOKER == 18 and platform_id("EvenBet") == EVENBET
    assert platform_id(None) == EVENBET
    with pytest.raises(ValueError):
        platform_id("HHPoker")
    assert table_key("128053412") == 128053412 and table_key(TABLE) == TABLE


async def test_pdecision_must_save_bluffpoker_hands_as_bluffpoker():
    def health(casino, platform):
        return lambda request: httpx.Response(200, json={"status": "ready", "handHistoryCasino": casino,
                                                         "handHistoryPlatform": platform,
                                                         "handHistoryUploadToAzure": True})
    ok = PDecision(client=httpx.AsyncClient(base_url="http://pd", transport=httpx.MockTransport(
        health("BluffPoker", "BluffPokerPlatform"))))
    assert (await ok.require_hand_history(BLUFFPOKER))["handHistoryCasino"] == "BluffPoker"
    legacy = PDecision(client=httpx.AsyncClient(base_url="http://pd", transport=httpx.MockTransport(
        health("BluffPoker", "RunYourOwnPokerSite"))))
    assert await legacy.require_hand_history(BLUFFPOKER)  # PCs registered before BluffPokerPlatform existed
    wrong = PDecision(client=httpx.AsyncClient(base_url="http://pd", transport=httpx.MockTransport(
        health("Evenbet", "EvenBet"))))
    with pytest.raises(RuntimeError, match="BluffPokerPlatform"):
        await wrong.require_hand_history(BLUFFPOKER)


async def test_one_pdecision_serves_the_hands_of_every_site_of_its_pc():
    body = {"status": "ready", "handHistoryCasino": "Evenbet", "handHistoryPlatform": "EvenBet", "handHistoryUploadToAzure": True,
            "casinos": [{"casino": "Evenbet", "platform": "EvenBet"}, {"casino": "BluffPoker", "platform": "BluffPokerPlatform"}]}
    both = PDecision(client=httpx.AsyncClient(base_url="http://pd", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=body))))
    assert await both.require_hand_history(BLUFFPOKER)
    assert await both.require_hand_history(EVENBET)


async def test_sessions_of_one_account_reuse_its_token_instead_of_logging_in_again():
    logins = []

    def handler(request):
        logins.append(request.url.path)
        return httpx.Response(200, json={"pokerToken": "a1", "refreshToken": "r1", "expiresIn": 3600, "clientUserId": PID})
    first = hub_with(handler)
    await first.login()
    second = hub_with(handler)  # e.g. the next lobby scan of the same account
    await second.login()
    assert logins == ["/integrations/clients/auth/login"] and second.player_id == PID and await second.token() == "a1"


def test_losing_a_watched_table_does_not_reconnect_the_account():
    c = client_at("preflop_turn")  # seated at TABLE
    watched = "6ab84bec-ddf7-4f60-a11a-9502d1e13ef0"
    c.states[watched] = state("preflop_turn") | {"s": [None] * 6}
    seated_sock, watched_sock = c.sockets[TABLE], bp.TableSocket(c, watched, watched)
    c.sockets[watched] = watched_sock
    c.table_lost(watched_sock, ConnectionError("closed"))
    assert watched not in c.sockets and watched not in c.states and c.connection.failure is None
    c.sockets[TABLE] = seated_sock = bp.TableSocket(c, TABLE, TABLE)
    c.table_lost(seated_sock, ConnectionError("closed"))
    assert isinstance(c.connection.failure, ConnectionError)  # a seated table: the session reconnects


def test_tournament_tables_never_look_like_ring_tables():
    raw = copy.deepcopy(DATA["states"]["preflop_turn"])
    raw["tournamentMeta"] = {"ante": 0}
    c = bp.BluffPokerClient(settings())
    c.player_id = PID
    sock = bp.TableSocket(c, TABLE, TABLE)
    assert c.on_table_state(sock, raw)
    assert "g" not in c.tables[TABLE] and c.tables[TABLE]["tournament"]["format"] == "SNG"


async def test_forgotten_table_is_no_longer_known():
    c = client_at("preflop_turn")
    c.sockets[TABLE] = bp.TableSocket(c, TABLE, TABLE)
    await c.forget_table(TABLE)
    assert TABLE not in c.tables and TABLE not in c.states and TABLE not in c.sockets


async def test_tournament_list_reads_every_page_of_the_hub():
    # The hub returns 20 tournaments, oldest first, unless asked for more: today's SNGs were on later pages.
    tournaments = [{"id": str(i), "type": "MTT", "status": "FINISHED"} for i in range(230)]
    pages = []

    def handler(request):
        if request.url.path.endswith("/login"):
            return httpx.Response(200, json={"pokerToken": "a1", "refreshToken": "r1", "expiresIn": 3600, "clientUserId": PID})
        take, skip = int(request.url.params["take"]), int(request.url.params["skip"])
        pages.append((take, skip))
        return httpx.Response(200, json={"tournaments": tournaments[skip:skip + take], "total": len(tournaments)})
    hub = hub_with(handler)
    await hub.login()
    listed = await hub.tournaments()
    assert [t["id"] for t in listed["tournaments"]] == [t["id"] for t in tournaments]
    assert pages == [(100, 0), (100, 100), (100, 200)]
