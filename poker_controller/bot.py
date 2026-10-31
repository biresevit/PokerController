from __future__ import annotations

import asyncio
import logging
import time

from .client import EvenBetClient
from .decision import PDecision
from .state import HandStateMapper, decision_to_play

log = logging.getLogger(__name__)


class Bot:
    def __init__(self, client: EvenBetClient, decisions: PDecision, table_id: int, *,
                 platform=11, hand_history=False, max_actions: int | None = None, control=None):
        if client.player_id is None:
            raise RuntimeError("Log in before creating a bot")
        self.client, self.decisions, self.table_id = client, decisions, table_id
        self.mapper = HandStateMapper(client.player_id, client.settings.username, platform)
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

    async def process(self, state: dict):
        async with self.action_lock:
            await self._process(state)

    async def _process(self, state: dict):
        hero = self.client.hero_seat(state)
        if hero is None:
            return
        hs = self.mapper.convert(state, self.client.tables.get(self.table_id, {}))
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
                        for frame in self.history_frames:
                            accepted = await self.decisions.record(frame)
                            if not accepted:
                                log.debug("PDecision skipped hand-history frame for hand %s", hand_id)
                        if await self.decisions.complete(self.history_frames[-1]):
                            self.history_complete.add(hand_id)
                            log.info("Completed hand history %s with %s frames", hand_id, len(self.history_frames))
                            self.history_frames = []
                        else:
                            log.error("PDecision could not complete hand history %s", hand_id)
        key = self.mapper.turn_key(state, hero)
        if not key or key in self.submitted:
            return
        # Do not ask for an obsolete queued snapshot; receiver continues while HTTP is pending.
        if self.mapper.turn_key(self.client.states[self.table_id], hero) != key:
            return
        if self.control:
            manual,command=await self.control.offer(key)
            if manual:
                self.manual_hand=state["gi"]
                if command:await self.control.play(self,self.client.states[self.table_id],hero,key,command)
                return
            # PDecision builds its decision tree from its own earlier decisions in the hand, so a
            # hand that had a manual turn stays manual; automatic play resumes at the next hand.
            if self.manual_hand==state["gi"]:
                return
        hs["IsNewHand"] = self.decision_hand != state["gi"]
        decision = await self.decisions.decide(hs)
        self.decision_hand = state["gi"]
        latest = self.client.states[self.table_id]
        latest_hero = self.client.hero_seat(latest)
        if latest_hero != hero or self.mapper.turn_key(latest, hero) != key:
            log.warning("Discarding stale decision for hand %s", state["gi"])
            return
        if decision.get("Action") == 6:
            log.error("PDecision returned Action=6 for hand %s; taking a free check or folding", state["gi"])
        if decision.get("Action") == 2 and 2 not in latest.get("aa", []) and 1 in latest.get("aa", []):
            log.error("PDecision returned CHECK for table %s hand %s, but legal actions are %s; folding",
                      self.table_id, state["gi"], latest.get("aa", []))
        action, funds = decision_to_play(decision, latest, hero)
        if self.control:
            if not await self.control.claim_auto(key):return
            latest=self.client.states[self.table_id]
            if self.client.hero_seat(latest)!=hero or self.mapper.turn_key(latest,hero)!=key:return
        # Record before sending: an ambiguous timeout must never cause a repeated monetary action.
        self.submitted.add(key)
        await self.client.play(self.table_id, action, funds)
        self.actions += 1
        log.info("Hand %s: PDecision %s -> EvenBet action=%s funds=%s", state["gi"], decision, action, funds)

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
