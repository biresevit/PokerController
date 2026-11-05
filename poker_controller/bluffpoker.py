"""BluffPoker player client behind the EvenBetClient interface.

A port of BluffPoker's player connector (bluffpoker-connector: test-bot-client and test-bot-runner/publicApi):
the hub's REST API (login, token refresh, lobby, table discovery, table tokens, seat buy-in/top-up, tournaments)
and one engine WebSocket per table (register, state, actions, sit in/out, leave).

Each BluffPoker tableState is translated into the EvenBet GameState shape that Bot, HandStateMapper, manual
control and the scheduler's Session already read, so the decision, hand-history and table logic is shared:

    ti table key            gi "<tableId>-<handSeq>"     gt game (72 NLH, 79 PLO4, 88 PLO5)
    s  seats: i player id, n/dn name, c stack, b street bet, d cards ("-1" hidden), f flags (1 sitting out)
    m  ai acting seat, di button, r street (1 preflop .. 4 river, 5 showdown)
    aa legal codes (1 fold, 2 check, 3 call, 8 bet, 9 raise)   f  c call, mn/mx raise bounds (additional chips)
    d  c board, p pot        ts 1 hand running, 3 hand over       bbv/sbv blinds       t.tr seconds left to act

Amounts are minor units like EvenBet's: cents at cash tables, whole chips at tournament tables (amount_scale).

The lobby list (GET /integrations/clients/tables, not wrapped by the connector) was read from the live hub:
{"tables": [{"id", "name", "gameType", "smallBlind", "bigBlind", "maxSeats", "activePlayers", "minBuyIn",
"maxBuyIn", "isActive", "isPrivate", "formats": {"doubleBoard", ...}, ...}], "total"}.
Not verified against a live BluffPoker server yet: 0- or 1-based seat numbers in other
table types (detected per state; cash tables are 0-based), and the sitOut command (the engine reports
sitOutResult; the connector only sends sitIn).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import os
import re
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

import httpx
from websockets.asyncio.client import connect as ws_connect

from .sites import BLUFFPOKER, table_key
from .transport import ProtocolError, redact

log = logging.getLogger(__name__)

GAMES = {"NLH": 72, "PLO4": 79, "PLO5": 88}
LIMITS = {"NLH": 80, "PLO4": 85, "PLO5": 85}  # EvenBet limit codes: 80 no limit, 85 pot limit
HOLE_COUNT = {"NLH": 2, "PLO4": 4, "PLO5": 5}
ROUNDS = {"idle": 0, "preflop": 1, "flop": 2, "turn": 3, "river": 4, "showdown": 5}
BETTING = {"preflop", "flop", "turn", "river"}
LEGAL = {"fold", "check", "call", "raise", "allin"}
RANKS, SUITS = "23456789TJQKA", "cdhs"
CARD = re.compile(r"^[2-9TJQKA][cdhs]$")
# Formats whose rules change what a legal decision is, or that PDecision cannot be given faithfully.
UNSUPPORTED = ("shortDeck", "doubleBoard", "dealersChoice", "flipGo", "allInOrFold", "fastFold", "bombPot", "bombPotHand")
TERMINAL_ERRORS = {"SESSION_SUPERSEDED", "UNKNOWN_TABLE", "PRIVATE_TABLE", "TABLE_SOCKET_MISMATCH", "NEED_PLAYER_ID"}
TERMINAL_CLOSE = {4000: "SESSION_REPLACED", 4401: "AUTH_FAILED", 4403: "ORIGIN_REJECTED"}

ACK_SECONDS = 5           # an action without actionResult in this time is ambiguous and never resent
COMMAND_SECONDS = 8       # sitIn / sitOut answers
LEAVE_ACK_SECONDS = 10
LEAVE_DEFERRED_SECONDS = 180  # a leave during a hand completes when the hand ends
HEARTBEAT_SECONDS = 15
SILENCE_SECONDS = 45
REFRESH_AHEAD_SECONDS = 60
# The stack each account left each table with (cents). Like most sites, BluffPoker refuses a rejoin below it
# (REJOIN_MIN_NOT_MET); kept for the whole worker process, across logins.
LEFT_WITH: dict[tuple[str, str], int] = {}


def default_config_path() -> Path:
    if os.name == "nt":
        return Path(r"C:\PokerSuite\bluffpoker.json")
    return Path.home() / ".pokersuite" / "bluffpoker.json"


class BluffPokerError(ProtocolError):
    """A refused or unconfirmed hub/engine request. `uncertain`: it may have been executed (network, 5xx, 408, 429)."""

    def __init__(self, code: str, *, status: int | None = None, topic: str = "BluffPoker",
                 terminal: bool = False, uncertain: bool = False, body=None):
        self.code, self.status, self.terminal, self.uncertain = code, status, terminal, uncertain
        self.body = body
        super().__init__({"t": topic, "text": code, "resultCode": status or 1})


def _http_url(value: str, name: str) -> str:
    parts = urlsplit(str(value or ""))
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parts.username or parts.password:
        raise ValueError(f"{name} must not contain credentials")
    return str(value).rstrip("/")


@dataclass(frozen=True)
class BluffPokerSettings:
    """One bot account on one BluffPoker environment. Secrets come from the PC's config file, never from code."""
    hub_url: str
    api_key: str = field(repr=False)
    origin: str
    username: str
    email: str = ""
    password: str = field(default="", repr=False)
    client_ip: str | None = None
    request_timeout: float = 15
    allow_static_credential: bool = False

    def __post_init__(self):
        object.__setattr__(self, "hub_url", _http_url(self.hub_url, "hub_url"))
        object.__setattr__(self, "origin", _http_url(self.origin, "origin"))
        if not self.api_key:
            raise ValueError("BluffPoker client API key is missing")
        if not self.username:
            raise ValueError("Bot username is missing")
        if not 0 < float(self.request_timeout) <= 120:
            raise ValueError("request_timeout must be between 0 and 120 seconds")

    @classmethod
    def load(cls, username: str, *, hub_url: str | None = None, path: str | Path | None = None,
             environ: dict | None = None) -> "BluffPokerSettings":
        """Settings for `username` from the config file, with environment overrides for the site-wide values.

        File (default C:\\PokerSuite\\bluffpoker.json, or BLUFFPOKER_CONFIG):
            {"hub_url": "...", "api_key": "...", "origin": "https://...", "client_ip": null,
             "accounts": {"BluffBot01": {"email": "...", "password": "..."}}}
        Environment: BP_HUB_BASE_URL, BP_CLIENT_API_KEY, BP_ORIGIN, BP_CLIENT_IP, BP_REQUEST_TIMEOUT_MS.
        `hub_url` (the site's EndpointUrl) wins over both.
        """
        env = os.environ if environ is None else environ
        config = Path(path or env.get("BLUFFPOKER_CONFIG") or default_config_path())
        data = json.loads(config.read_text(encoding="utf-8-sig")) if config.exists() else {}
        if not isinstance(data, dict):
            raise ValueError(f"{config} must contain a JSON object")
        accounts = data.get("accounts") or {}
        account = next((v for k, v in accounts.items() if str(k).casefold() == username.casefold()), None) or {}
        timeout_ms = env.get("BP_REQUEST_TIMEOUT_MS")
        values = {
            "hub_url": hub_url or env.get("BP_HUB_BASE_URL") or data.get("hub_url"),
            "api_key": env.get("BP_CLIENT_API_KEY") or data.get("api_key"),
            "origin": env.get("BP_ORIGIN") or data.get("origin"),
            "client_ip": env.get("BP_CLIENT_IP") or data.get("client_ip") or None,
        }
        missing = [k for k in ("hub_url", "api_key", "origin") if not values[k]]
        if missing:
            raise ValueError(f"BluffPoker {', '.join(missing)} not configured: set them in {config} or the environment")
        return cls(username=username, email=account.get("email") or "", password=account.get("password") or "",
                   request_timeout=float(timeout_ms) / 1000 if timeout_ms else float(data.get("request_timeout") or 15),
                   allow_static_credential=bool(data.get("allow_static_credential")), **values)


# ---------------------------------------------------------------- amounts and cards

def to_cents(value) -> int | None:
    """Exact non-negative chip amount in hundredths (number or decimal string with at most 2 decimals)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        m = re.fullmatch(r"(\d+)(?:\.(\d{1,2}))?", value.strip())
        return int(m.group(1)) * 100 + int((m.group(2) or "").ljust(2, "0")) if m else None
    if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
        scaled = value * 100
        rounded = round(scaled)
        return int(rounded) if abs(scaled - rounded) <= 1e-6 else None
    return None


def cap_to_cents(value) -> int | None:
    """An upper bound in cents, never above the value received (the engine publishes 24.089999999999996)."""
    cents = to_cents(value)
    if cents is None or isinstance(value, str):
        return cents
    return cents - 1 if cents / 100 > value else cents


def chips(cents: int) -> float:
    return round(cents / 100, 2)


def card_index(code: str) -> int:
    """BluffPoker card ("As") as EvenBet's card index (rank * 4 + suit, ranks 2..A, suits c d h s)."""
    return RANKS.index(code[0].upper()) * 4 + SUITS.index(code[1].lower())


def card_list(codes) -> str:
    return ";".join(str(card_index(c)) for c in codes)


def valid_cards(value) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(c, str) and CARD.match(c) for c in value):
        return None
    return list(value)


def variant(st: dict) -> str | None:
    preset = str(st.get("preset") or "").upper()
    game, holes, betting = st.get("game"), st.get("holeCount"), st.get("betting")
    if game == "HOLDEM" and holes == 2 and betting == "NL" and "PLO" not in preset:
        return "NLH"
    if game == "OMAHA" and betting == "PL" and holes in (4, 5):
        return f"PLO{holes}"
    return None


def _cents(value, reason="BAD_AMOUNT") -> int:
    cents = to_cents(0 if value is None else value)
    if cents is None:
        raise BluffPokerError(reason, topic="tableState")
    return cents


def _cap(value) -> int:
    cents = cap_to_cents(value)
    if cents is None:
        raise BluffPokerError("BAD_AMOUNT", topic="tableState")
    return cents


def _raise_bounds(st: dict, me: dict, tournament: bool, game: str) -> tuple[int, int, int] | None:
    """(min, max, all-in) raise-to totals in cents, as the connector's legalizer computes them."""
    allin_total = _cents(me.get("betThisRound")) + _cents(me.get("stack"))
    cap = allin_total
    if game != "NLH":
        limit = st.get("potLimitMaxTotal")
        if limit is None:
            return None
        cap = min(cap, _cap(limit))
    if st.get("minRaiseTotal") is None:
        return None
    step = 100 if tournament else 1
    minimum = math.ceil(_cents(st["minRaiseTotal"]) / step) * step
    maximum = allin_total if cap >= allin_total else cap // step * step
    return minimum, maximum, allin_total


def game_state(st: dict, player_id: str, *, key, now: float | None = None) -> dict:
    """Translate one BluffPoker tableState into the EvenBet GameState shape (see the module docstring)."""
    if not isinstance(st, dict) or not isinstance(st.get("seats"), list):
        raise BluffPokerError("STATE_NOT_OBJECT", topic="tableState")
    for flag in UNSUPPORTED:
        if st.get(flag) is True:
            raise BluffPokerError(f"UNSUPPORTED_FORMAT_{flag}", topic="tableState", terminal=True)
    if isinstance(st.get("boardB"), list) and st["boardB"]:
        raise BluffPokerError("UNSUPPORTED_FORMAT_secondBoard", topic="tableState", terminal=True)
    game = variant(st)
    if game is None:
        raise BluffPokerError("UNKNOWN_VARIANT", topic="tableState", terminal=True)
    phase = st.get("phase")
    if phase not in ROUNDS:
        raise BluffPokerError("UNKNOWN_PHASE", topic="tableState")
    hand = st.get("handSeq")
    if type(hand) is not int or hand < 0:
        raise BluffPokerError("BAD_HAND_SEQ", topic="tableState")
    board = valid_cards(st.get("board") or [])
    if board is None:
        raise BluffPokerError("BAD_BOARD", topic="tableState")
    run = st.get("tournamentRun") if isinstance(st.get("tournamentRun"), dict) else st.get("tournamentMeta")
    tournament = isinstance(run, dict)
    unit = 100 if tournament else 1  # cents per minor unit: cash cents, tournament whole chips
    ante = st.get("ante", st.get("anteAmount", run.get("ante") if tournament else None))
    holes = HOLE_COUNT[game]

    raw_seats = st["seats"]
    numbers = [s.get("seat") for s in raw_seats if isinstance(s, dict)]
    if len(numbers) != len(raw_seats) or not all(type(n) is int and n >= 0 for n in numbers):
        raise BluffPokerError("BAD_SEAT", topic="tableState")
    max_seats = st.get("maxSeats") if type(st.get("maxSeats")) is int and st["maxSeats"] > 0 else None
    # Seat numbers are 0-based (seatIndex); a state listing seats 1..maxSeats is read as 1-based.
    base = 1 if numbers and min(numbers) >= 1 and max_seats and max(numbers) == max_seats else 0
    size = max([max_seats or 0] + [n - base + 1 for n in numbers])
    seats: list[dict | None] = [None] * size
    me, hero = None, None
    street_bets = 0
    for s in raw_seats:
        index = s["seat"] - base
        if s.get("empty") is True or not isinstance(s.get("playerId"), str):
            continue
        mine = s["playerId"] == player_id
        street_bets += _cents(s.get("betThisRound"))
        if mine and me is not None:
            raise BluffPokerError("DUPLICATE_OWN_SEAT", topic="tableState")
        hole = valid_cards(s.get("holeCards") or []) or []
        in_hand = s.get("dealtIn") is True and s.get("folded") is not True
        dealt = (card_list(hole) if len(hole) == holes else ";".join(["-1"] * holes)) if in_hand else ""
        name = s.get("screenName") if isinstance(s.get("screenName"), str) and 0 < len(s["screenName"]) <= 64 else ""
        seats[index] = {"i": s["playerId"], "n": name or s["playerId"], "dn": name,
                        "c": _cents(s.get("stack")) // unit, "b": _cents(s.get("betThisRound")) // unit,
                        "d": dealt, "f": 1 if s.get("sittingOut") is True or s.get("isSittingOut") is True else 0}
        if mine:
            me, hero = s, index

    actor = st.get("currentActorSeat")
    actor = actor - base if type(actor) is int else -1
    street_max = _cents(st.get("streetMaxBet"))
    codes: set[int] = set()
    funds = {"c": 0, "mn": 0, "mx": 0}
    legal: list[str] = []
    my_bet = stack = 0
    if me is not None:
        my_bet, stack = _cents(me.get("betThisRound")), _cents(me.get("stack"))
    if me is not None and phase in BETTING and actor == hero:
        raw_legal = me.get("legalActions") if isinstance(me.get("legalActions"), list) else []
        if not all(a in LEGAL for a in raw_legal):
            raise BluffPokerError("UNKNOWN_LEGAL_ACTION", topic="tableState")
        legal = sorted(set(raw_legal))
        aggressive = 9 if street_max > 0 else 8
        to_call = max(0, street_max - my_bet)
        bounds = _raise_bounds(st, me, tournament, game) if {"raise", "allin"} & set(legal) else None
        allin_fits = game == "NLH" or (st.get("potLimitMaxTotal") is not None and
                                       my_bet + stack <= _cap(st["potLimitMaxTotal"]))
        if "fold" in legal:
            codes.add(1)
        if "check" in legal:
            codes.add(2)
        if "call" in legal or ("allin" in legal and 0 < stack <= to_call):
            codes.add(3)  # a call for the whole stack is the engine's all-in
            funds["c"] = min(to_call, stack) // unit
        if "raise" in legal and bounds and bounds[0] <= bounds[1]:
            codes.add(aggressive)
            funds["mn"], funds["mx"] = (bounds[0] - my_bet) // unit, (bounds[1] - my_bet) // unit
        elif "allin" in legal and allin_fits and stack > to_call:
            codes.add(aggressive)
            funds["mn"] = funds["mx"] = stack // unit
    clock = st.get("actionClockEndsAt")
    left = None
    if isinstance(clock, (int, float)) and math.isfinite(clock):
        left = max(0.0, (clock - (time.time() if now is None else now) * 1000) / 1000)
    return {
        "ti": key, "gi": f"{st.get('tableId')}-{hand}", "gt": GAMES[game],
        "s": seats,
        "m": {"ai": actor, "di": st.get("button") - base if type(st.get("button")) is int else 0, "r": ROUNDS[phase]},
        "aa": sorted(codes), "f": funds,
        # The engine's pot includes the street's bets; PDecision's Pot (EvenBet's d.p) does not.
        "d": {"c": card_list(board), "p": max(0, _cents(st.get("pot")) - street_bets) // unit},
        # The first idle state after a hand already carries the stacks after the payout (live, 4 Nov 2026).
        "ts": 1 if phase in BETTING else 3 if phase == "showdown" or (phase == "idle" and hand > 0) else 0,
        "bbv": _cents(st.get("bigBlind")) // unit, "sbv": _cents(st.get("smallBlind")) // unit,
        "t": {"tr": left, "tb": 0} if left is not None else {},
        # Engine facts the client needs to turn an EvenBet action back into a BluffPoker one.
        "bp": {"hand": hand, "game": game, "tournament": tournament, "unit": unit, "base": base,
               "ante": _cents(ante) // unit,
               "legal": legal, "my_bet": my_bet, "stack": stack, "street_max": street_max, "phase": phase},
    }


def rejoin_minimum(body) -> int | None:
    """The amount a REJOIN_MIN_NOT_MET refusal requires, in cents:
    {"error": "REJOIN_MIN_NOT_MET", "rejoin": {"required": 174.5, "max": 200, "leftStack": 174.5, ...}}."""
    rejoin = body.get("rejoin") if isinstance(body, dict) else None
    if not isinstance(rejoin, dict):
        return None
    return to_cents(rejoin.get("required")) or to_cents(rejoin.get("leftStack")) or None


def _first(row: dict, *names):
    return next((row[n] for n in names if row.get(n) is not None), None)


def lobby_row(row: dict) -> dict | None:
    """A lobby table in the EvenBet row shape (amounts in cents), or None for a table the bots do not play."""
    if not isinstance(row, dict):
        return None
    wire = _first(row, "tableId", "id")
    if not isinstance(wire, (str, int)) or str(wire) == "":
        return None
    game = str(_first(row, "gameType", "variant") or "").upper() or variant(row)
    formats = row.get("formats") if isinstance(row.get("formats"), dict) else {}
    if game not in GAMES or any(row.get(f) is True or formats.get(f) is True for f in UNSUPPORTED):
        return None
    if row.get("isPrivate") is True or row.get("isActive") is False:
        return None  # password tables and closed tables are not played
    if row.get("tournamentRun") or row.get("tournamentId") or str(row.get("mode") or "").lower() == "tournament":
        return None
    def amount(*names):
        value = _first(row, *names)
        return to_cents(value) if value is not None else None
    seats = _first(row, "maxSeats", "seatCount", "seats")
    players = _first(row, "activePlayers", "playerCount", "seatedCount", "occupiedSeats", "players")
    if isinstance(players, list):
        players = len(players)
    return {"i": table_key(wire), "wire": str(wire), "n": str(_first(row, "name", "tableName") or wire),
            "g": GAMES[game], "p": LIMITS[game],
            "bb": amount("bigBlind", "bb") or 0, "s": amount("smallBlind", "sb") or 0,
            "mp": seats if type(seats) is int and seats > 0 else None,
            "np": players if type(players) is int and players >= 0 else 0,
            "mn": amount("minBuyIn", "buyInMin", "minimumBuyIn"), "mx": amount("maxBuyIn", "buyInMax", "maximumBuyIn")}


def _rows(data, *names) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for name in names:
            if isinstance(data.get(name), list):
                return data[name]
    raise BluffPokerError("BAD_RESPONSE", uncertain=True)


# ---------------------------------------------------------------- hub

class Credential:
    """One account's access/refresh pair, shared by every Hub of that account in this process."""

    def __init__(self):
        self.access: str | None = None
        self.refresh_token: str | None = None
        self.expires = 0.0
        self.player_id: str | None = None
        self.failed: str | None = None
        self.lock = asyncio.Lock()


# The hub allows 30 logins per 15 minutes per IP: sessions of an account (lobby scans, plays, reconnects) reuse
# its pair and refresh it, they do not log in again.
CREDENTIALS: dict[tuple[str, str], Credential] = {}


class Hub:
    """The hub's REST API for one account: one access/refresh pair, refreshed single-flight before it expires."""

    def __init__(self, settings: BluffPokerSettings, http: httpx.AsyncClient | None = None, clock=time.monotonic):
        self.settings, self.clock = settings, clock
        self.http = http or httpx.AsyncClient(base_url=settings.hub_url, timeout=settings.request_timeout)
        self.credential = CREDENTIALS.setdefault((settings.hub_url, (settings.email or settings.username).casefold()),
                                                 Credential())

    def __getattr__(self, name):
        if name in ("access", "refresh_token", "expires", "player_id", "failed", "lock"):
            return getattr(self.__dict__["credential"], name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        if name in ("access", "refresh_token", "expires", "player_id", "failed"):
            setattr(self.credential, name, value)
        else:
            super().__setattr__(name, value)

    def headers(self, bearer=None, idempotency=None):
        h = {"x-api-key": self.settings.api_key, "Origin": self.settings.origin}
        if self.settings.client_ip:
            h["X-BP-Client-IP"] = self.settings.client_ip
        if bearer:
            h["Authorization"] = f"Bearer {bearer}"
        if idempotency:
            h["X-Idempotency-Key"] = idempotency
        return h

    async def call(self, method, path, *, body=None, bearer=None, idempotency=None, topic=None):
        topic = topic or path
        try:
            response = await self.http.request(method, path, json=body,
                                               headers=self.headers(bearer, idempotency))
        except httpx.TimeoutException:
            raise BluffPokerError("TIMEOUT", topic=topic, uncertain=True) from None
        except httpx.HTTPError:
            raise BluffPokerError("NETWORK", topic=topic, uncertain=True) from None
        try:
            data = response.json() if response.content else {}
        except ValueError:
            data = None
        status = response.status_code
        if not 200 <= status < 300:
            code = next((data[k] for k in ("error", "code") if isinstance(data, dict) and isinstance(data.get(k), str)
                         and data[k]), f"HTTP_{status}")
            retryable = status >= 500 or status in (408, 429)
            raise BluffPokerError(code, status=status, topic=topic, terminal=not retryable, uncertain=retryable,
                                  body=data)
        if not isinstance(data, (dict, list)):
            raise BluffPokerError("BAD_RESPONSE", status=status, topic=topic, uncertain=True)
        if isinstance(data, dict) and data.get("ok") is False:
            raise BluffPokerError("PROOF_MISMATCH", status=status, topic=topic, uncertain=True)
        return data

    def adopt(self, access, refresh, expires_in):
        if not (isinstance(access, str) and access and isinstance(refresh, str) and refresh
                and isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool)):
            raise BluffPokerError("BAD_RESPONSE", topic="auth", uncertain=True)
        self.access, self.refresh_token, self.expires = access, refresh, self.clock() + float(expires_in)

    async def login(self):
        """Log in, or reuse the account's pair while it is valid (it is refreshed before it expires)."""
        if self.access and not self.failed and self.expires - self.clock() > REFRESH_AHEAD_SECONDS:
            return {"clientUserId": self.player_id, "reused": True}
        s = self.settings
        if not s.email or not s.password:
            raise BluffPokerError("CREDENTIALS_NOT_CONFIGURED", topic="login", terminal=True)
        data = await self.call("POST", "/integrations/clients/auth/login",
                               body={"email": s.email, "password": s.password}, topic="login")
        if not isinstance(data, dict) or not isinstance(data.get("clientUserId"), str) or not data["clientUserId"]:
            raise BluffPokerError("BAD_RESPONSE", topic="login", uncertain=True)
        self.adopt(data.get("pokerToken"), data.get("refreshToken"), data.get("expiresIn"))
        self.player_id, self.failed = data["clientUserId"], None
        return data

    async def token(self) -> str:
        if self.failed:
            raise BluffPokerError(self.failed, topic="auth", terminal=True)
        if self.access is None:
            raise BluffPokerError("NOT_LOGGED_IN", topic="auth", terminal=True)
        if self.expires - self.clock() > REFRESH_AHEAD_SECONDS:
            return self.access
        return await self.refresh(self.access)

    async def refresh(self, observed: str) -> str:
        """Single-flight: a token already rotated by a concurrent caller is returned without a second refresh."""
        async with self.lock:
            if self.failed:
                raise BluffPokerError(self.failed, topic="auth", terminal=True)
            if self.access != observed and self.expires - self.clock() > REFRESH_AHEAD_SECONDS:
                return self.access
            try:
                data = await self.call("POST", "/auth/refresh",
                                       body={"refreshToken": self.refresh_token, "accessToken": self.access},
                                       topic="refresh")
            except BluffPokerError as error:
                if error.terminal:
                    self.failed = "CREDENTIAL_EXPIRED"  # refused pair: no silent re-login
                raise
            # Both tokens from the same answer, or nothing.
            self.adopt(data.get("accessToken") or data.get("pokerToken"), data.get("refreshToken"), data.get("expiresIn"))
            return self.access

    async def authed(self, method, path, **kw):
        token = await self.token()
        try:
            return await self.call(method, path, bearer=token, **kw)
        except BluffPokerError as error:
            if error.status != 401:
                raise
        return await self.call(method, path, bearer=await self.refresh(token), **kw)

    async def tables(self):
        return await self.call("GET", "/integrations/clients/tables", topic="tables")

    async def endpoint(self, table_id: str, game: str | None = None) -> str:
        """WebSocket URL of a table with its credential: discovery, then a short-lived table token."""
        tid = quote(str(table_id), safe="")
        c = await self.call("GET", f"/integrations/clients/tables/{tid}/connection", topic="connection")
        mode = c.get("engineAuthMode") if isinstance(c, dict) else None
        if not isinstance(c.get("wsUrl"), str) or str(c.get("tableId")) != str(table_id) or mode not in ("jwt", "static"):
            raise BluffPokerError("DISCOVERY_BAD_RESPONSE", topic="connection", terminal=True)
        if game and isinstance(c.get("gameType"), str) and c["gameType"] and c["gameType"].upper() != game:
            raise BluffPokerError("DISCOVERY_VARIANT_MISMATCH", topic="connection", terminal=True)
        parts = urlsplit(c["wsUrl"])
        if parts.scheme not in ("ws", "wss") or not parts.netloc:
            raise BluffPokerError("DISCOVERY_BAD_WS_URL", topic="connection", terminal=True)
        if parts.username or parts.password:
            raise BluffPokerError("DISCOVERY_WS_URL_USERINFO", topic="connection", terminal=True)
        query = dict(parse_qsl(parts.query))
        if mode == "static":
            if not self.settings.allow_static_credential:
                raise BluffPokerError("STATIC_CREDENTIAL_NOT_ALLOWED", topic="connection", terminal=True)
            if not c.get("engineThumbKey"):
                raise BluffPokerError("STATIC_CREDENTIAL_MISSING", topic="connection", terminal=True)
            query["thumbKey"] = c["engineThumbKey"]
        else:
            t = await self.authed("POST", f"/integrations/clients/tables/{tid}/ws-token", topic="ws-token")
            if not isinstance(t, dict) or not isinstance(t.get("wsToken"), str) or not t["wsToken"]:
                raise BluffPokerError("WS_TOKEN_BAD_RESPONSE", topic="ws-token", uncertain=True)
            query["token"] = t["wsToken"]  # the table JWT, never the account token
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))

    async def buy_in_seat(self, table_id: str, seat: int, cents: int, key: str) -> dict:
        data = await self.authed("POST", "/api/poker-wallet/buy-in-seat", idempotency=key, topic="buy-in-seat",
                                 body={"tableId": str(table_id), "seatIndex": seat, "amount": chips(cents)})
        engine = data.get("engine") if isinstance(data, dict) else None
        if not isinstance(engine, dict) or engine.get("ok") is not True or engine.get("error") is not None:
            raise BluffPokerError("UNVERIFIED_RESPONSE", topic="buy-in-seat", uncertain=True)
        if data.get("ok") is not True or to_cents(data.get("reservedChips")) != cents or \
                (engine.get("seat") is not None and engine["seat"] != seat):
            raise BluffPokerError("PROOF_MISMATCH", topic="buy-in-seat", uncertain=True)
        return data

    async def top_up_seat(self, table_id: str, seat: int, cents: int, key: str) -> dict:
        data = await self.authed("POST", "/api/poker-wallet/top-up-seat", idempotency=key, topic="top-up-seat",
                                 body={"tableId": str(table_id), "seatIndex": seat, "amount": chips(cents)})
        if not isinstance(data, dict) or data.get("ok") is not True or to_cents(data.get("debited")) != cents:
            raise BluffPokerError("PROOF_MISMATCH", topic="top-up-seat", uncertain=True)
        return data

    async def summary(self):
        return await self.authed("GET", "/player/account/summary", topic="summary")

    async def tournaments(self):
        return await self.authed("GET", "/player/tournaments", topic="tournaments")

    async def tournament(self, tournament_id):
        return await self.authed("GET", f"/player/tournaments/{quote(str(tournament_id), safe='')}", topic="tournament")

    async def register(self, tournament_id):
        # No idempotency on this route: a register is an entry (or a re-entry) and is never blindly retried.
        return await self.authed("POST", f"/player/tournaments/{quote(str(tournament_id), safe='')}/register",
                                 topic="register")

    async def unregister(self, tournament_id):
        return await self.authed("DELETE", f"/player/tournaments/{quote(str(tournament_id), safe='')}/register",
                                 topic="unregister")

    async def tournament_state(self, tournament_id):
        return await self.authed("GET", f"/player/tournaments/{quote(str(tournament_id), safe='')}/state",
                                 topic="tournament-state")

    async def close(self):
        await self.http.aclose()


# ---------------------------------------------------------------- engine

class TableSocket:
    """One engine WebSocket for one table. Actions are never resent: an answer that cannot be attributed
    (no actionResult in time, or the engine's clock acted while our action was pending) stays ambiguous."""

    def __init__(self, client: "BluffPokerClient", key, wire: str):
        self.client, self.key, self.wire = client, key, wire
        self.ws = None
        self.receiver: asyncio.Task | None = None
        self.pinger: asyncio.Task | None = None
        self.first_state = asyncio.Event()
        self.results: deque = deque()
        self.clock_results = 0
        self.unproven = False
        self.commands: dict[str, asyncio.Future] = {}
        self.leave_ack: asyncio.Future | None = None
        self.leave_done: asyncio.Future | None = None
        self.leaving = self.closed = False
        self.last_inbound = time.monotonic()
        self.tasks: set[asyncio.Task] = set()

    def later(self, message: dict):
        """Send from the receive loop without blocking it."""
        task = asyncio.create_task(self.send(message))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def open(self, timeout: float = 20):
        info = self.client.tables.get(self.key, {})
        game = {v: k for k, v in GAMES.items()}.get(info.get("g"))
        url = await self.client.hub.endpoint(self.wire, game)
        self.ws = await self.client.ws_connect(url, origin=self.client.settings.origin, open_timeout=timeout,
                                               ping_interval=20, ping_timeout=20, max_size=16 * 1024 * 1024)
        self.receiver = asyncio.create_task(self.receive(), name=f"bluffpoker-{self.wire}")
        await self.send({"type": "register", "playerId": self.client.player_id, "tableId": self.wire})
        async with asyncio.timeout(timeout):
            await self.first_state.wait()
        self.pinger = asyncio.create_task(self.heartbeat(), name=f"bluffpoker-ping-{self.wire}")
        return self

    async def send(self, message: dict) -> bool:
        if self.ws is None or self.closed:
            return False
        log.debug("send %s", redact(message))
        try:
            await self.ws.send(json.dumps(message, separators=(",", ":")))
        except Exception:
            return False
        return True

    async def receive(self):
        try:
            async for raw in self.ws:
                self.last_inbound = time.monotonic()
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    log.debug("recv %s", redact(message))
                    self.handle(message)
            code = getattr(self.ws, "close_code", None)
            raise BluffPokerError(TERMINAL_CLOSE.get(code, f"CLOSED_{code}"), topic="socket",
                                  terminal=code in TERMINAL_CLOSE)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.lost(exc)

    def handle(self, m: dict):
        kind = m.get("type")
        if isinstance(m.get("tableId"), str) and m["tableId"] != self.wire and kind != "error":
            return  # a late broadcast of another table
        if kind == "registered":
            self.later({"type": "getState", "tableId": self.wire})
        elif kind == "tableState":
            if self.client.on_table_state(self, m.get("state")):
                self.first_state.set()
        elif kind == "actionResult":
            self.on_result(m)
        elif kind == "playerAutoFolded" and m.get("playerId") == self.client.player_id:
            # The engine's clock acted for us; its actionResult is not an answer to any write of ours.
            if self.results:
                self.give_up_results("ENGINE_CLOCK_ACTED")
            else:
                self.clock_results += 1
        elif kind in ("sitInResult", "sitOutResult"):
            future = self.commands.pop(kind[:-6], None)
            if future and not future.done():
                future.set_result(m)
        elif kind in ("leaveTableResult", "leave_rejected", "seatVacated"):
            self.on_leave(m)
        elif kind == "error":
            code = str(m.get("code") or "ENGINE_ERROR")
            log.warning("BluffPoker engine error at table %s: %s", self.wire, code)
            if code == "TOURNAMENT_NO_LEAVE":
                self.on_leave({"type": "leaveTableResult", "ok": False, "error": code})
            elif code in TERMINAL_ERRORS:
                self.lost(BluffPokerError(code, topic="engine", terminal=True))
                task = asyncio.create_task(self.close())
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)

    def on_result(self, m: dict):
        if self.unproven:
            return
        if self.clock_results:
            self.clock_results -= 1
            return
        if self.results:
            future = self.results.popleft()
            if not future.done():
                future.set_result(m)

    def give_up_results(self, reason: str):
        """From here on no actionResult on this connection is attributed to a write: they stay ambiguous."""
        if not self.unproven:
            log.warning("BluffPoker action results at table %s can no longer be attributed: %s", self.wire, reason)
        self.unproven = True
        while self.results:
            future = self.results.popleft()
            if not future.done():
                future.set_result(None)

    async def action(self, payload: dict, timeout: float = ACK_SECONDS) -> dict | None:
        future = asyncio.get_running_loop().create_future()
        if not self.unproven:
            self.results.append(future)
        if not await self.send(payload):
            if future in self.results:
                self.results.remove(future)
            raise BluffPokerError("SEND_NOT_WRITTEN", topic="action")
        if self.unproven:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout)
        except TimeoutError:
            self.give_up_results("ACK_WINDOW_PASSED")
            return None

    async def command(self, kind: str, timeout: float = COMMAND_SECONDS) -> dict:
        future = asyncio.get_running_loop().create_future()
        old = self.commands.get(kind)
        if old and not old.done():
            old.cancel()
        self.commands[kind] = future
        if not await self.send({"type": kind, "tableId": self.wire}):
            raise BluffPokerError("SEND_NOT_WRITTEN", topic=kind)
        try:
            reply = await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise BluffPokerError("NO_ANSWER", topic=kind, uncertain=True) from None
        if reply.get("ok") is not True:
            raise BluffPokerError(str(reply.get("error") or "REFUSED"), topic=kind)
        return reply

    def on_leave(self, m: dict):
        kind = m.get("type")
        if kind == "seatVacated":
            if m.get("playerId") != self.client.player_id:
                return
            for future in (self.leave_ack, self.leave_done):
                if future and not future.done():
                    future.set_result("left")
            return
        ack = self.leave_ack
        if not ack or ack.done():
            return
        if kind == "leave_rejected":
            ack.set_exception(BluffPokerError(str(m.get("reason") or "LEAVE_REJECTED"), topic="leave"))
        elif m.get("ok") is True and m.get("pending") is True:
            ack.set_result("pending")  # deferred to the end of the hand: seatVacated follows
        elif m.get("ok") is True or m.get("error") == "NOT_SEATED":
            ack.set_result("left")
            if self.leave_done and not self.leave_done.done():
                self.leave_done.set_result("left")
        else:
            ack.set_exception(BluffPokerError(str(m.get("error") or "LEAVE_REJECTED"), topic="leave"))

    async def leave(self, key: str) -> str:
        loop = asyncio.get_running_loop()
        self.leaving = True
        self.leave_ack, self.leave_done = loop.create_future(), loop.create_future()
        if not await self.send({"type": "playerLeaveTable", "tableId": self.wire, "idempotencyKey": key}):
            raise BluffPokerError("LEAVE_NOT_SENT", topic="leave")
        outcome = await asyncio.wait_for(self.leave_ack, LEAVE_ACK_SECONDS)
        if outcome == "pending":
            try:
                await asyncio.wait_for(self.leave_done, LEAVE_DEFERRED_SECONDS)
            except TimeoutError:
                raise BluffPokerError("SEAT_NOT_VACATED_IN_TIME", topic="leave", uncertain=True) from None
            return "left_after_hand"
        return "left"

    async def heartbeat(self):
        while not self.closed:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            if time.monotonic() - self.last_inbound > SILENCE_SECONDS:
                self.lost(BluffPokerError("HEARTBEAT_TIMEOUT", topic="socket"))
                await self.close()
                return
            await self.send({"type": "ping"})

    def lost(self, exc: Exception):
        for future in [*self.results, *self.commands.values(), self.leave_ack, self.leave_done]:
            if future and not future.done():
                future.set_exception(exc)
        self.results.clear()
        if not self.leaving and not self.closed:
            self.client.table_lost(self, exc)

    async def close(self):
        if self.closed:
            return
        self.closed = True
        current = asyncio.current_task()
        for task in (self.pinger, self.receiver):
            if task and task is not current:
                task.cancel()
        if self.ws is not None:
            try:
                await self.ws.close()
            except Exception:
                pass
        for future in [*self.results, *self.commands.values(), self.leave_ack, self.leave_done]:
            if future and not future.done():
                future.cancel()


class BluffPokerConnection:
    """The event side of EvenBet's Connection: translated GameState events from every table socket."""

    def __init__(self):
        self.listeners = []
        self.notifications: asyncio.Queue = asyncio.Queue(maxsize=4096)
        self.failure: Exception | None = None
        self.sequence = 0

    def emit(self, message: dict):
        for listener in self.listeners:
            listener(message)
        if self.notifications.full():
            self.notifications.get_nowait()  # snapshots: the newest one is what counts
        self.notifications.put_nowait(message)

    def fail(self, exc: Exception):
        if self.failure is None:
            self.failure = exc
            if self.notifications.full():
                self.notifications.get_nowait()
            self.notifications.put_nowait(exc)

    async def next_event(self) -> dict:
        if self.failure and self.notifications.empty():
            raise self.failure
        item = await self.notifications.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def request(self, topic: str, **fields):
        raise BluffPokerError("UNSUPPORTED_REQUEST", topic=topic)

    async def send(self, topic: str, **fields):
        raise BluffPokerError("UNSUPPORTED_REQUEST", topic=topic)

    async def close(self):
        return None


class BluffPokerClient:
    """One bot account on BluffPoker, with EvenBetClient's interface (see the module docstring)."""
    platform = BLUFFPOKER

    def __init__(self, settings: BluffPokerSettings, *, hub: Hub | None = None, ws_connect=ws_connect):
        self.settings = settings
        self.hub = hub or Hub(settings)
        self.ws_connect = ws_connect
        self.connection = BluffPokerConnection()
        self.player_id: str | None = None
        self.tables: dict = {}
        self.tournaments: dict = {}
        self.states: dict = {}
        self.state_versions: dict = {}
        self.state_times: dict = {}
        self.results: dict = {}
        self.table_entry_indices: dict = {}
        self.tournament_entry_indices: dict = {}
        self.table_select_indices: dict = {}
        self.sockets: dict = {}
        self.changed = asyncio.Event()
        self.auth = ""
        self.chat_events: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.chat_overflow = False
        self.run_id = uuid.uuid4().hex[:12]
        self.leave_counts: dict = {}
        self.buy_in_warned: set = set()

    # -- state

    def wire(self, table_id) -> str:
        key = table_key(table_id)
        return self.tables.get(key, {}).get("wire") or str(table_id)

    def on_table_state(self, sock: TableSocket, raw) -> bool:
        try:
            state = game_state(raw, self.player_id, key=sock.key)
        except BluffPokerError as error:
            log.warning("BluffPoker state of table %s not usable: %s", sock.wire, error.code)
            if error.terminal:
                sock.lost(error)
            return False
        info = self.tables.setdefault(sock.key, {"i": sock.key, "wire": sock.wire, "n": sock.wire})
        if not info.get("mp"):
            info["mp"] = len(state["s"])
        info.setdefault("g", state["gt"])
        info.setdefault("p", LIMITS.get(state["bp"]["game"], 80))
        info.setdefault("bb", state["bbv"])
        info.setdefault("s", state["sbv"])
        info["an"] = state["bp"]["ante"]
        if state["bp"]["tournament"]:
            info.setdefault("tournament", {"format": "SNG"})
        self.states[sock.key] = state
        self.state_versions[sock.key] = self.state_versions.get(sock.key, 0) + 1
        self.state_times[sock.key] = time.time()
        self.changed.set()
        self.connection.emit({"t": "GameState", "gameState": copy.deepcopy(state)})
        return True

    def table_lost(self, sock: TableSocket, exc: Exception):
        if self.sockets.get(sock.key) is not sock:
            return
        self.sockets.pop(sock.key, None)
        if self.hero_seat(self.states.get(sock.key, {})) is None:
            # A table only watched (lobby scan): forget it, the next join opens it again. Reconnecting the
            # account would interrupt its seated tables for nothing.
            self.states.pop(sock.key, None)
            log.info("BluffPoker watched table connection closed: %s (%s)", sock.wire, exc)
            return
        log.warning("BluffPoker table connection lost: %s (%s)", sock.wire, exc)
        self.connection.fail(exc)

    def hero_seat(self, state: dict) -> int | None:
        return next((i for i, seat in enumerate(state.get("s", []))
                     if seat and seat.get("i") == self.player_id and not seat.get("f", 0) & 128), None)

    async def wait_state(self, table_id, predicate=lambda s: True, timeout: float = 20) -> dict:
        key = table_key(table_id)
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                if key in self.states and predicate(self.states[key]):
                    return copy.deepcopy(self.states[key])
                if self.connection.failure:
                    raise self.connection.failure
                try:
                    await asyncio.wait_for(self.changed.wait(), 0.5)
                except TimeoutError:
                    pass

    def socket(self, table_id) -> TableSocket:
        sock = self.sockets.get(table_key(table_id))
        if sock is None or sock.closed:
            raise BluffPokerError("NOT_AT_TABLE", topic="table")
        return sock

    # -- session

    async def open(self, *, login: bool = True):
        if login:
            await self.hub.login()
            self.player_id = self.hub.player_id
        return self

    async def refresh_player_tables(self) -> list:
        return []

    async def list_tables(self) -> list[dict]:
        rows = [r for r in (lobby_row(x) for x in _rows(await self.hub.tables(), "tables", "items", "data")) if r]
        for row in rows:
            self.tables.setdefault(row["i"], {}).update({k: v for k, v in row.items() if v is not None})
        return [self.tables[r["i"]].copy() for r in rows]

    async def list_tournaments(self) -> list[dict]:
        rows = _rows(await self.hub.tournaments(), "tournaments", "items", "data")
        self.tournaments = {str(r.get("tournamentId") or r.get("id")): r for r in rows if isinstance(r, dict)}
        return [dict(r) for r in self.tournaments.values()]

    async def join(self, table_id, password: str = "") -> dict:
        """Open the table's socket (or ask an open one for a fresh state) and wait for its state."""
        key = table_key(table_id)
        sock = self.sockets.get(key)
        if sock is None or sock.closed:
            sock = TableSocket(self, key, self.wire(key))
            self.sockets[key] = sock
            try:
                await sock.open()
            except BaseException:
                self.sockets.pop(key, None)
                await sock.close()
                raise
        else:
            version = self.state_versions.get(key, 0)
            await sock.send({"type": "getState", "tableId": sock.wire})
            await self.wait_state(key, lambda s: self.state_versions.get(key, 0) > version)
        return {"ok": True}

    async def buy_in_range(self, table_id) -> dict:
        """Buy-in limits in cents, from the lobby; 40 to 100 big blinds when the lobby does not publish them."""
        key = table_key(table_id)
        info = self.tables.get(key, {})
        minimum, maximum = info.get("mn"), info.get("mx")
        if minimum is None or maximum is None:
            bb = info.get("bb") or self.states.get(key, {}).get("bbv") or 0
            if key not in self.buy_in_warned:
                self.buy_in_warned.add(key)
                log.warning("BluffPoker lobby has no buy-in limits for table %s; using 40-100 big blinds", self.wire(key))
            minimum, maximum = 40 * bb, 100 * bb
        return {"minAmount": int(minimum), "maxAmount": int(maximum)}

    async def balance(self) -> dict:
        return await self.hub.summary()

    # -- seat

    def idempotency(self, kind: str, table_id) -> str:
        return f"{self.run_id}:{kind}:{self.wire(table_id)}:{uuid.uuid4().hex}"

    async def money(self, call, *args):
        """A seat payment with its idempotency key: an unconfirmed attempt is retried once with the same key,
        which the hub answers with the first attempt's result instead of paying twice."""
        try:
            return await call(*args)
        except BluffPokerError as error:
            if not error.uncertain:
                raise
            log.warning("BluffPoker payment unconfirmed (%s); asking again with the same key", error.code)
            return await call(*args)

    async def sit(self, table_id, chips_amount, seat: int | None = None, *, wait_big_blind: bool = False) -> int:
        if self.player_id is None:
            raise RuntimeError("Login required")
        key = table_key(table_id)
        state = await self.wait_state(key)
        existing = self.hero_seat(state)
        if existing is not None:
            if state["s"][existing].get("f", 0) & 1:
                await self.sit_in(key)
            return existing  # Never buy in twice after attaching to an existing seat.
        seats = state["s"]
        if seat is None:
            seat = next((i for i, s in enumerate(seats) if s is None), None)
        if seat is None or not 0 <= seat < len(seats) or seats[seat] is not None:
            raise ValueError("Requested seat is not free")
        cents = to_cents(str(chips_amount))
        if cents is None or cents <= 0:
            raise ValueError("Buy-in must be a positive amount with at most two decimal places")
        limits = await self.buy_in_range(key)
        if not limits["minAmount"] <= cents <= limits["maxAmount"]:
            raise ValueError(f"Buy-in must be between {limits['minAmount']/100:g} and {limits['maxAmount']/100:g} chips")
        base = state.get("bp", {}).get("base", 0)
        try:
            await self.money(self.hub.buy_in_seat, self.wire(key), seat + base, cents, self.idempotency("buy-in", key))
        except BluffPokerError as error:
            if error.code != "REJOIN_MIN_NOT_MET":
                raise
            # A recent leave with more chips: the table only takes the bot back with exactly that stack.
            required = rejoin_minimum(error.body) or LEFT_WITH.get((self.settings.username, self.wire(key)))
            if not required or required == cents:
                log.warning("BluffPoker refused the rejoin of %s at %s without a known amount: %s",
                            self.settings.username, self.wire(key), error.body)
                raise
            log.info("Rejoin of %s at %s requires the stack it left with: buying in %s instead of %s",
                     self.settings.username, self.wire(key), chips(required), chips(cents))
            await self.money(self.hub.buy_in_seat, self.wire(key), seat + base, required,
                             self.idempotency("buy-in", key))
        state = await self.wait_state(key, lambda s: self.hero_seat(s) is not None and
                                      s["s"][self.hero_seat(s)].get("c", 0) > 0)
        hero = self.hero_seat(state)
        if state["s"][hero].get("f", 0) & 1:
            await self.sit_in(key)
        return hero

    async def rebuy(self, table_id, chips_amount, *, wait_big_blind: bool = False) -> int:
        key = table_key(table_id)
        state = self.states.get(key, {})
        seat = self.hero_seat(state)
        if seat is None:
            raise ValueError("The hero is no longer seated")
        cents = to_cents(str(chips_amount))
        if cents is None or cents <= 0:
            raise ValueError("Rebuy must be a positive amount with at most two decimal places")
        limits = await self.buy_in_range(key)
        if not limits["minAmount"] <= cents <= limits["maxAmount"]:
            raise ValueError(f"Rebuy must be between {limits['minAmount']/100:g} and {limits['maxAmount']/100:g} chips")
        base = state.get("bp", {}).get("base", 0)
        await self.money(self.hub.top_up_seat, self.wire(key), seat + base, cents, self.idempotency("top-up", key))
        # Credited at once when idle, or when the hand in progress ends.
        await self.wait_state(key, lambda s: self.hero_seat(s) == seat and s["s"][seat].get("c", 0) > 0, timeout=120)
        hero_state = self.states[key]
        if hero_state["s"][seat].get("f", 0) & 1:
            await self.sit_in(key)
        return seat

    async def take_tournament_seat(self, table_id) -> int | None:
        """BluffPoker seats tournament players itself; opening the table is enough."""
        return self.hero_seat(self.states.get(table_key(table_id), {}))

    async def sit_in(self, table_id):
        return await self.socket(table_id).command("sitIn")

    async def player_command(self, table_id, command: int):
        """EvenBet player commands that have a BluffPoker counterpart: 10 back in play, 47 sit out next hand."""
        if command == 10:
            state = self.states.get(table_key(table_id), {})
            hero = self.hero_seat(state)
            if hero is not None and state["s"][hero].get("f", 0) & 1:
                return await self.sit_in(table_id)
            return {}
        if command == 47:
            return await self.socket(table_id).command("sitOut")
        return {}  # e.g. run-it-twice answers: the engine applies its own default

    async def return_from_away(self, table_id):
        return await self.sit_in(table_id)

    async def sit_out_next_hand(self, table_id):
        return await self.player_command(table_id, 47)

    async def sit_back(self, table_id):
        return await self.sit_in(table_id)

    async def leave(self, table_id):
        """Leave the seat (deferred by the engine to the end of a hand in progress) and close the table socket."""
        key = table_key(table_id)
        sock = self.sockets.get(key)
        if sock is None or sock.closed:
            return {"ok": True, "outcome": "not_connected"}
        try:
            state = self.states.get(key, {})
            hero = self.hero_seat(state)
            if hero is None:
                return {"ok": True, "outcome": "not_seated"}
            seat = state["s"][hero]
            unit = state["bp"]["unit"]
            self.leave_counts[key] = self.leave_counts.get(key, 0) + 1
            # One key per leave of this run: a retry of the same leave reuses it, a later leave gets a new one.
            outcome = await sock.leave(f"{self.run_id}:{self.player_id}:{sock.wire}:{self.leave_counts[key]}:leave")
            # The final stack: after a deferred leave the last state before the seat emptied.
            final = self.states.get(key, state)
            mine = final["s"][self.hero_seat(final)] if self.hero_seat(final) is not None else seat
            if not state["bp"]["tournament"]:
                LEFT_WITH[(self.settings.username, sock.wire)] = (mine.get("c", 0) + mine.get("b", 0)) * unit
            return {"ok": True, "outcome": outcome}
        finally:
            if self.sockets.get(key) is sock:
                self.sockets.pop(key, None)
            await sock.close()
            self.states.pop(key, None)

    async def forget_table(self, table_id):
        """Close a table's socket without leaving its seat (a tournament moved the account, or it is out)."""
        key = table_key(table_id)
        sock = self.sockets.pop(key, None)
        self.states.pop(key, None)
        if sock:
            await sock.close()

    async def play(self, table_id, action: int, funds: int = 0):
        """Send an EvenBet action (1 fold, 2 check, 3 call, 8 bet, 9 raise + additional chips) as BluffPoker's
        fold/check/call/raise-to/allin. Never resent; an unanswered action is logged as ambiguous."""
        key = table_key(table_id)
        state = self.states.get(key)
        if not state:
            raise BluffPokerError("NO_STATE", topic="action")
        bp = state["bp"]
        legal = set(bp["legal"])
        message = {"type": "action", "tableId": self.wire(key)}
        if action == 1:
            message["action"] = "fold"
        elif action == 2:
            message["action"] = "check"
        elif action == 3:
            message["action"] = "call" if "call" in legal else "allin"
        elif action in (8, 9):
            total = bp["my_bet"] + int(funds) * bp["unit"]
            if total >= bp["my_bet"] + bp["stack"] or "raise" not in legal:
                if "allin" not in legal:
                    raise ValueError(f"Raise to {total} cents is not possible: {sorted(legal)}")
                message["action"] = "allin"
            else:
                message.update(action="raise", amount=chips(total))  # the street total ("raise to")
        elif action == 10:
            return {}  # showing a won hand has no BluffPoker command
        else:
            raise ValueError(f"Unsupported action {action}")
        if message["action"] not in legal:
            raise ValueError(f"BluffPoker action {message['action']} is not legal: {sorted(legal)}")
        reply = await self.socket(key).action(message)
        if reply is None:
            log.warning("No attributable actionResult for %s at table %s hand %s", message["action"], self.wire(key),
                        bp["hand"])
            return {"ok": None}
        if reply.get("ok") is not True:
            raise BluffPokerError(str(reply.get("error") or "ACTION_REJECTED"), topic="actionResult")
        return reply

    # -- chat (no player chat route in the connector)

    async def read_chat(self, table_id):
        return None

    async def send_chat(self, table_id, text: str):
        raise ValueError("Table chat is not available on BluffPoker")

    # -- tournaments (REST; the server seats the player at state.myTableId)

    async def register_tournament(self, tournament_id):
        return await self.hub.register(tournament_id)

    async def unregister_tournament(self, tournament_id):
        return await self.hub.unregister(tournament_id)

    async def tournament_state(self, tournament_id):
        return await self.hub.tournament_state(tournament_id)

    # -- lifecycle

    async def close(self):
        """Close every table socket. No logout: it would release the account's seats (a reconnecting session or a
        restarted worker keeps them) and discard the token the account's next session reuses."""
        sockets, self.sockets = list(self.sockets.values()), {}
        for sock in sockets:
            await sock.close()
        await self.hub.close()

    async def __aenter__(self):
        try:
            return await self.open()
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()

