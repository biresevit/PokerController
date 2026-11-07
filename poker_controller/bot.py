from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .client import EvenBetClient  # or BluffPokerClient: the same interface
from .decision import PDecision
from .state import HandStateMapper, amount_scale, decision_to_play

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, client: EvenBetClient, decisions: PDecision, table_id: int, *,
                 platform=None, hand_history=False, max_actions: int | None = None, control=None, on_bust=None,
                 on_show_offer=None):
        if client.player_id is None:
            raise RuntimeError("Log in before creating a bot")
        self.client, self.decisions, self.table_id = client, decisions, table_id
        self.mapper = HandStateMapper(client.player_id, client.settings.username,
                                      platform if platform is not None else getattr(client, "platform", 11))
        self.hand_history, self.max_actions = hand_history, max_actions
        self.history_hand: int | None = None
        self.history_frames: list[dict] = []
        self.history_complete: set[int] = set()
        self.submitted: set[str] = set()
        self.actions = 0
        self.decision_hand: int | None = None
        self.manual_hand: int | None = None
        self.control=control
        self.action_lock=asyncio.Lock()
        # Set while the scheduler takes the seat away on purpose; the bot must then stay away.
        self.leaving=False
        self.table_detached=False
        self.sit_out_requested=False
        self.last_return=0.0
        # Called once each time the hero loses the whole stack; the owner rebuys or leaves.
        self.on_bust=on_bust
        self.busted=False
        # Set while the account takes a planned break; the bot must stay away until it ends.
        self.on_break=False
        # Called once per hand when the table lets the hero show cards after an uncontested win.
        self.on_show_offer=on_show_offer
        self.show_offer_hand=None

    async def process(self, state: dict):
        async with self.action_lock:
            await self._process(state)

    async def _process(self, state: dict):
        if self.table_detached:return
        hero = self.client.hero_seat(state)
        if hero is None:
            return
        seat = state["s"][hero]
        if seat.get("c", 0) > 0 or seat.get("b", 0) > 0:
            self.busted = False
        elif not seat.get("d") or state.get("ts") == 3:
            # No chips behind or in front and not in a hand: an all-in was lost.
            if not self.busted and self.on_bust and not self.leaving:
                self.busted = True
                self.on_bust(self.table_id)
        if seat.get("f", 0) & 1 and not self.busted:
            self.sit_out_requested = False
            await self.return_if_unintended(state)
        if (self.on_show_offer and state.get("m", {}).get("ai") == hero and 10 in state.get("aa", [])
                and self.show_offer_hand != state.get("gi")):
            self.show_offer_hand = state.get("gi")
            self.on_show_offer(self.table_id, state, hero)
        table = self.client.tables.get(self.table_id, {})
        scale = amount_scale(table)
        hs = self.mapper.convert(state, table)
        if hs is None:
            return
        if self.hand_history:
            hand_id = state["gi"]
            if self.history_hand != hand_id:
                self.history_hand = hand_id
                self.history_frames = []
            if hand_id not in self.history_complete:
                # A bot can attach after a hand has begun. Such a fragment must
                # not be presented to PDecision as a complete hand history.
                if self.history_frames or (state.get("ts") == 1 and
                                           state.get("m", {}).get("r") == 1 and
                                           not state.get("d", {}).get("c")):
                    self.history_frames.append(hs.copy())
                if state.get("ts") == 3:
                    if self.history_frames and any(hs["Winners"]):
                        try:
                            for frame in self.history_frames:
                                accepted = await self.decisions.record(frame)
                                if not accepted:
                                    log.debug("PDecision skipped hand-history frame for hand %s", hand_id)
                            completed = await self.decisions.complete(self.history_frames[-1])
                        except httpx.HTTPError as exc:
                            # PDecision unavailable even after the retries (e.g. restarting): this hand's history
                            # is lost, but the table worker must keep playing.
                            log.error("Hand history %s not saved, PDecision unavailable (%s)", hand_id, type(exc).__name__)
                            self.history_complete.add(hand_id)
                            self.history_frames = []
                            completed = None
                        if completed:
                            self.history_complete.add(hand_id)
                            log.info("Completed hand history %s with %s frames", hand_id, len(self.history_frames))
                            self.history_frames = []
                        elif completed is not None:
                            log.error("PDecision could not complete hand history %s", hand_id)
        key = self.mapper.turn_key(state, hero)
        if not key or key in self.submitted:
            return
        # Do not ask for an obsolete queued snapshot; receiver continues while HTTP is pending.
        if self.mapper.turn_key(self.client.states[self.table_id], hero) != key:
            return
        if self.control:
            try:
                manual,command=await self.control.offer(key)
            except Exception as exc:
                # The manual-control check needs the database; while it is unreachable nobody can take the
                # table over either, so the bot plays the turn instead of stopping the table worker.
                log.warning("Manual control check failed at table %s (%s); playing automatically", self.table_id, exc)
                manual,command=False,None
            if manual:
                self.manual_hand=state["gi"]
                if command:await self.control.play(self,self.client.states[self.table_id],hero,key,command)
                return
            # PDecision builds its decision tree from its own earlier decisions in the hand, so a
            # hand that had a manual turn stays manual; automatic play resumes at the next hand.
            if self.manual_hand==state["gi"]:
                return
        hs["IsNewHand"] = self.decision_hand != state["gi"]
        try:
            decision = await self.decisions.decide(hs)
        except httpx.HTTPError as exc:
            # PDecision unavailable even after the retries: play the turn safely instead of stopping the table
            # worker, which would leave the bot seated and timing out every hand.
            log.error("PDecision unavailable for table %s hand %s (%s); taking a free check or folding",
                      self.table_id, state["gi"], type(exc).__name__)
            decision = {"Action": 6, "Fallback": True}
        self.decision_hand = state["gi"]
        latest = self.client.states[self.table_id]
        if self.table_detached:return
        latest_hero = self.client.hero_seat(latest)
        if latest_hero != hero or self.mapper.turn_key(latest, hero) != key:
            log.warning("Discarding stale decision for hand %s", state["gi"])
            return
        if decision.get("Action") == 6 and not decision.get("Fallback"):
            log.error("PDecision returned Action=6 for hand %s; taking a free check or folding", state["gi"])
        if decision.get("Action") == 2 and 2 not in latest.get("aa", []) and 1 in latest.get("aa", []):
            log.error("PDecision returned CHECK for table %s hand %s, but legal actions are %s; folding",
                      self.table_id, state["gi"], latest.get("aa", []))
        try:
            action, funds = decision_to_play(decision, latest, hero, scale=scale)
        except ValueError as exc:
            # One unplayable answer must not stop the table worker: the bot would then time out
            # every following hand (and a tournament bot can never leave). Take the safe action.
            log.error("Cannot play PDecision %s for table %s hand %s (%s); taking a free check or folding",
                      decision, self.table_id, state["gi"], exc)
            action, funds = decision_to_play({"Action": 6}, latest, hero, scale=scale)
        if self.control:
            try:
                claimed=await self.control.claim_auto(key)
            except Exception as exc:
                log.warning("Turn claim failed at table %s (%s); playing automatically", self.table_id, exc)
                claimed=True
            if not claimed:return
            latest=self.client.states[self.table_id]
            if self.table_detached or self.client.hero_seat(latest)!=hero or self.mapper.turn_key(latest,hero)!=key:return
        # Record before sending: an ambiguous timeout must never cause a repeated monetary action.
        self.submitted.add(key)
        await self.client.play(self.table_id, action, funds)
        self.actions += 1
        log.info("Hand %s: PDecision %s -> EvenBet action=%s funds=%s", state["gi"], decision, action, funds)

    async def return_if_unintended(self, state: dict):
        """Press "I'm back" after the table sat the hero out, e.g. after a timed-out turn.

        Leaving the table is an intentional break, and under manual control the human decides.
        """
        if self.leaving or self.on_break or time.monotonic() - self.last_return < 10:
            return
        self.last_return = time.monotonic()
        try:
            if self.control and await self.control.manual_enabled():
                return
            latest = self.client.states.get(self.table_id, state)
            hero = self.client.hero_seat(latest)
            if hero is None or not latest["s"][hero].get("f", 0) & 1:
                return
            await self.client.sit_back(self.table_id)
            log.info("Hero was sitting out at table %s; pressed I'm back", self.table_id)
        except Exception:
            log.exception("Could not return from sitting out at table %s", self.table_id)

    async def run(self, seconds: float = 120):
        await self.decisions.health()
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.max_actions is not None and self.actions >= self.max_actions:
                return
            try:
                event = await asyncio.wait_for(self.client.connection.next_event(), min(1, max(0.01, deadline-time.monotonic())))
            except TimeoutError:
                continue
            if event.get("t") == "GameState":
                state = event.get("gameState", event.get("GameState", {}))
                if state.get("ti") == self.table_id:
                    await self.process(state)
            elif event.get("t") == "NotifyFromServer" and event.get("resultCode"):
                raise RuntimeError(f"Server notification: {event.get('text', event.get('resultCode'))}")
