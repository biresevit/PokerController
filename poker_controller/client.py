from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass
from decimal import Decimal

from .transport import Connection


@dataclass(frozen=True)
class Settings:
    url: str = "wss://web.evenbetpoker.com/front"
    origin: str = "https://demo.evenbetpoker.com"
    skin: str = "evenbetpoker"
    username: str = "monica"
    password: str = "poker"
    protocol_version: int = 740


def to_funds(chips: str | float | Decimal) -> int:
    amount = Decimal(str(chips)) * 100
    if not amount.is_finite() or amount < 0 or amount != amount.to_integral_value():
        raise ValueError("Amount must be nonnegative with at most two decimal places")
    return int(amount)


class EvenBetClient:
    """Database-free session. The frontend currently proxies table messages to game servers."""
    def __init__(self, settings: Settings = Settings(), connection: Connection | None = None):
        self.settings = settings
        self.connection = connection or Connection(settings.url, settings.origin)
        self.connection.listeners.append(self._on_message)
        self.player_id: int | None = None
        self.tables: dict[int, dict] = {}
        self.states: dict[int, dict] = {}
        self.state_versions: dict[int, int] = {}
        self.table_entry_indices: dict[int, int] = {}
        self.table_select_indices: dict[int, int] = {}
        self.changed = asyncio.Event()
        self.auth = ""
        self.chat_events = asyncio.Queue(maxsize=2000)
        self.chat_history_requests = set()
        self.chat_overflow = False
        self.chat_waiters = {}

    def _on_message(self, message: dict):
        topic = message.get("t")
        if topic == "Chat":
            from .chat import incoming_chat
            if message.get("srcMsgId") in self.chat_history_requests:
                message={**message,"history":True}
                self.chat_history_requests.discard(message.get("srcMsgId"))
            for item in incoming_chat(message):
                key=(int(item["table_id"]),item["body"])
                waiter=self.chat_waiters.get(key)
                if waiter and not waiter[0].done() and not item["historical"] and item["author"].casefold() in waiter[1]:
                    waiter[0].set_result(True)
                try: self.chat_events.put_nowait(item)
                except asyncio.QueueFull: self.chat_overflow=True
        elif topic == "AuthState":
            self.auth = message.get("auth", "")
        elif topic == "GameState":
            state = message.get("gameState", message.get("GameState", {}))
            if state.get("ti"):
                table_id = state["ti"]
                self.states[table_id] = copy.deepcopy(state)
                self.state_versions[table_id] = self.state_versions.get(table_id, 0) + 1
                self.changed.set()
        elif topic == "GoToTable" and "info" in message:
            info = message["info"]
            self.tables.setdefault(info["i"], {}).update(info)
        elif topic == "TableState" and self.player_id is not None:
            table_id = message.get("tableId")
            seat = next((s for s in message.get("seats", [])
                         if s and s.get("pi") == self.player_id), None)
            if table_id and seat and seat.get("ei") is not None:
                self.table_entry_indices[int(table_id)] = int(seat["ei"])

    def player_entry_idx(self, table_id: int) -> int:
        return self.table_entry_indices.get(table_id, 0)

    async def open(self, *, login: bool = True):
        await self.connection.open()
        await self.connection.request("ClientVersion", clientVersion="HTML5", locale="en",
                                      protocolVersion=self.settings.protocol_version, skinName=self.settings.skin)
        if login:
            reply = await self.connection.request("Login", login=self.settings.username,
                password=self.settings.password, oldAuth="", tfaAuth="", tfaType=95,
                timeZone="Europe/Budapest", loginParam="{}", secondary=False, remember=False)
            self.player_id = int(reply.get("playerId") or reply["text"])
            await self.refresh_player_tables()
        return self

    async def refresh_player_tables(self) -> list[dict]:
        if self.player_id is None:
            return []
        reply = await self.connection.request("GetPlayerTablesByPlayerId",
                                              synchronous=True, playerId=self.player_id)
        tables = reply.get("tables") or []
        indices = reply.get("playerEntryIdxs") or []
        for table, index in zip(tables, indices):
            if table.get("i") is not None:
                self.table_select_indices[int(table["i"])] = int(index)
        return tables

    async def list_tables(self) -> list[dict]:
        reply = await self.connection.request("GetTablesList", gameType=0)
        tables = reply.get("tables", [])
        # Subsequent replies contain partial rows (e.g. only i/np). Preserve
        # game, stakes and other metadata from the initial full snapshot.
        for table in tables:
            self.tables.setdefault(table["i"], {}).update(table)
        return [self.tables[t["i"]].copy() for t in tables]

    async def join(self, table_id: int, password: str = "") -> dict:
        entry_idx=self.table_select_indices.get(table_id,0)
        if table_id in self.table_select_indices:
            await self.connection.request("GetTableState", tableId=table_id,
                                          playerEntryIdx=entry_idx, isRealTable=False)
            entry_idx=self.player_entry_idx(table_id)
        reply = await self.connection.request("SelectTable", tableId=table_id,
                                              playerEntryIdx=entry_idx, password=password)
        await self.connection.request("GetTableState", tableId=table_id,
                                      playerEntryIdx=entry_idx, isRealTable=False)
        await self.wait_state(table_id)
        return reply

    async def wait_state(self, table_id: int, predicate=lambda s: True, timeout: float = 20) -> dict:
        async with asyncio.timeout(timeout):
            while True:
                self.changed.clear()
                if table_id in self.states and predicate(self.states[table_id]):
                    return copy.deepcopy(self.states[table_id])
                if self.connection.failure:
                    raise self.connection.failure
                # Also notice a disconnect when no further GameState arrives.
                try:
                    await asyncio.wait_for(self.changed.wait(), 0.5)
                except TimeoutError:
                    pass

    async def buy_in_range(self, table_id: int) -> dict:
        return await self.connection.request("TableSitDownBuyInRangeRequest", tableId=table_id, playerEntryIdx=0)

    async def balance(self) -> dict:
        return await self.connection.request("GetBalance")

    async def player_command(self, table_id: int, command: int):
        return await self.connection.request("PlayerCommand", tableId=table_id, playerEntryIdx=self.player_entry_idx(table_id),
            command=command, amount1=0, amount2=0, cardIndexes=[])

    async def return_from_away(self, table_id: int):
        return await self.connection.request("ReturnFromAway", tableId=table_id,
                                             playerEntryIdx=self.player_entry_idx(table_id))

    def hero_seat(self, state: dict) -> int | None:
        return next((i for i, seat in enumerate(state.get("s", []))
                     if seat and seat.get("i") == self.player_id and not seat.get("f", 0) & 128), None)

    async def sit(self, table_id: int, chips: str | float, seat: int | None = None, *, wait_big_blind: bool = False) -> int:
        if self.player_id is None:
            raise RuntimeError("Login required")
        state = await self.wait_state(table_id)
        existing = self.hero_seat(state)
        if existing is not None:
            if state["s"][existing].get("f", 0) & 1:
                await self.return_from_away(table_id)
            await self.player_command(table_id, 10)
            return existing  # Never buy in twice after attaching to an existing seat.
        seats = state["s"]
        if seat is None:
            seat = next((i for i, s in enumerate(seats) if s is None), None)
        if seat is None or not 0 <= seat < len(seats) or seats[seat] is not None:
            raise ValueError("Requested seat is not free")
        funds = to_funds(chips)
        limits = await self.buy_in_range(table_id)
        if not limits["minAmount"] <= funds <= limits["maxAmount"]:
            raise ValueError(f"Buy-in must be between {limits['minAmount']/100:g} and {limits['maxAmount']/100:g} chips")
        await self.connection.request("ReserveSeat", tableId=table_id, playerEntryIdx=0, seatIdx=seat)
        try:
            await self.connection.request("SitDown", tableId=table_id, playerEntryIdx=0, seatIdx=seat,
                cash=funds, playerCommands=[36, 12, 4, 2 if wait_big_blind else 1, 10], acceptedExchangeRate=0,
                acceptedPrecision=0, entryCnt=1, password="", paymentClubId=0,
                returnAfterLeaveWithoutCalculation=False, srcCurrencyId=0, useCurrencyConversion=False)
            await self.wait_state(table_id, lambda s: self.hero_seat(s) is not None and
                                  (s["s"][self.hero_seat(s)].get("c", 0) > 0))
            await self.player_command(table_id, 10)
        except Exception:
            # Release reservations if the server rejects the buy-in.
            try:
                await self.connection.request("UnreserveSeat", tableId=table_id, playerEntryIdx=0, seatIdx=seat)
            except Exception:
                pass
            raise
        return seat

    async def read_chat(self, table_id: int):
        request_id=self.connection.sequence+1
        self.chat_history_requests.add(request_id)
        try:
            await self.connection.send("Chat",history=True,tableId=table_id,
                playerEntryIdx=self.player_entry_idx(table_id),chatMessage=[])
        except BaseException:
            self.chat_history_requests.discard(request_id)
            raise

    async def send_chat(self, table_id: int, text: str):
        if not isinstance(text,str) or not text.strip() or len(text.encode("utf-16-le"))>4000:
            raise ValueError("A chat message must contain 1 to 2000 characters")
        if self.hero_seat(self.states.get(table_id,{})) is None:
            raise ValueError("The selected bot is no longer seated at this table")
        from datetime import datetime,timezone
        state=self.states[table_id];hero=state["s"][self.hero_seat(state)]
        authors={str(value).casefold() for value in (self.settings.username,hero.get("dn"),hero.get("n")) if value}
        key=(table_id,text.strip())
        if key in self.chat_waiters:
            raise ValueError("This message is already being sent")
        future=asyncio.get_running_loop().create_future()
        self.chat_waiters[key]=(future,authors)
        try:
            await self.connection.send("Chat",history=False,tableId=table_id,
                playerEntryIdx=self.player_entry_idx(table_id),chatMessage=[{
                    "a":self.settings.username,"c":"","i":288521,"l":0,"t":1,
                    "m":text.strip(),"r":datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}])
            await asyncio.wait_for(future,8)
        finally:
            self.chat_waiters.pop(key,None)

    async def leave(self, table_id: int):
        return await self.connection.request("LeaveTable", tableId=table_id,
            playerEntryIdx=self.player_entry_idx(table_id), tbtLeaveMethod=95)

    async def play(self, table_id: int, action: int, funds: int = 0):
        return await self.connection.request("Play", tableId=table_id,
            playerEntryIdx=self.player_entry_idx(table_id), action=action, funds=funds)

    async def close(self):
        await self.connection.close()

    async def __aenter__(self):
        try:
            return await self.open()
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *_):
        await self.close()
