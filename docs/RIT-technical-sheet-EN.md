# BluffPoker - Run It Twice (RIT): technical sheet for bot integrators

Audience: Nenad (PokerSuite agent). Written 2026-11-12, read-only analysis of the code that runs in production.

Legend: **[V]** verified in the BluffPoker source (file:line). **[N]** only Nenad can confirm in his own logs/code (we cannot see his agent).
Source revision: worktree `BluffPoker-avatar-stable` at `e2319aa4`. The engine and the hub are unchanged since `ee295ba6` (`git diff --name-only ee295ba6 e2319aa4 -- engine services/newbook-auth-hub` is empty), and the later commits are player-frontend only. We did not touch the VPS, so environment overrides on the server (for example `RIT_CONSENT_MS`) are not verified: always trust the `timeoutMs`/`totalMs` fields of the offer, not the default below.
Paths: `ENG/` = `engine/game-core/src/`, `HUB/` = `services/newbook-auth-hub/src/`, `CLI/` = `services/test-bot-client/src/`.

## 0. Summary (read this first)

1. RIT is a **consent vote over the game WebSocket (engine)**. There is no HTTP endpoint and no decision-API call for it. [V]
2. The engine sends `run_it_twice_offer` to every player still in the hand when betting is closed by an all-in. Everyone must answer `accepted:true` for two boards. A decline, or no answer inside the window (default **15 s**), means **one board** and the hand goes on. [V]
3. **Our reference bot client (the code in the connector package we sent) does not answer the offer.** It ignores the message (`default: return`), so on a RIT-enabled table every all-in against a bot ends with `reason:"timeout"` after the full window, and the whole table waits for it. [V] CLI/tableActor.ts:666-667 (the connector package README, "Server -> client" list, does not list the RIT messages either).
4. Whether the PokerSuite agent reads the offer and sends the reply depends on his connector, which we have not seen. [N]
5. Tables only offer RIT when the table setting `runItMultiTimeEnabled` is true. It is off unless the operator switches it on (the admin form default is false). [V]

## 1. When the offer is created (engine to client flow)

### 1.1 Preconditions (all must hold) [V]
Decision point: `FlopGameTable._advanceStreet()` ENG/flopGameTable.js:3500-3544, called when a betting round closes (ENG/flopGameTable.js:3431-3435).

| # | Condition | Source |
|---|---|---|
| a | Betting is closed and **at most one** non-folded player still has chips, with **at least two** non-folded players (all-in runout, "reveal lock") | ENG/flopGameTable.js:3508-3513 |
| b | Phase is **preflop, flop or turn** (on the river there is nothing to run: showdown) | ENG/flopGameTable.js:3525-3527, 3533 |
| c | Table setting `runItMultiTimeEnabled` is true | ENG/flopGameTable.js:3532; default absent/off (ENG/flopGameTable.js:159-174; hub type HUB/types/tableOptions.ts:44; admin form default false, `table-form-dialog.tsx:107`) |
| d | The table has **no board B** (`!this.boardB`): a **double-board table never gets an RIT offer** (its boardB is an empty array `[]`, which is truthy, so the check fails) | ENG/flopGameTable.js:3534; reset rules ENG/flopGameTable.js:2499-2505, docs/RIT_KNOWN_ISSUES.md |
| e | The deck still holds **two full runouts including burns**: `deckLeft >= 2 * cards(phase)`, cards = preflop 8, flop 4, turn 2 | ENG/seatCapacity.js:53-64, ENG/flopGameTable.js:3535 |
| f | The hand was not already decided at the deal (everyone all-in on blinds/antes, Flip & Go, bomb-pot ante): those hands are settled inside `startHand()` and run once, **no offer** | ENG/flopGameTable.js:2555, 2660, 2699 (`ritOffer:false`); ENG/flopGameTable.js:3088-3101 |

Who is "involved": every non-folded player of the hand (the all-in players and the player with chips) - `playersInvolved` ENG/flopGameTable.js:3540. Folded players and spectators do not get the offer. Number of players: any (>= 2 non-folded), see the deck rule for the ceiling.

### 1.2 Variants and number of players [V]
The same code serves all variants; only the deck rule (e) differs (cards left = 52 minus hole cards dealt):
- **NLH** (2 hole cards): 52-2n >= 16 for n <= 8, so always offered, including 8 players preflop.
- **PLO4** (4 hole cards): 52-4n >= 16 for n <= 8 (20 left with 8 players), so always offered.
- **PLO5** (5 hole cards): preflop needs 16 cards left, so offered up to **7 dealt-in players** (17 left). With **8 dealt-in players** (12 left) there is **no preflop offer**, but the flop (8 left, needs 8) and turn all-ins are offered. Evidence: engine matrix test `engine/game-core/test/integration/ritConsentMatrix.test.js:121-133, 163`.
- Table maximum is 8 seats (ENG/seatCapacity.js:10).
- Dealer's choice, short-deck, tournaments: no special-casing of RIT was found in `ENG/` (the same flag and deck rule apply); not exercised by us. [N/untested]

### 1.3 Who receives what, and when [V]
1. Betting closes: `applyAction` returns `{ritPending:true, playersInvolved}` (ENG/flopGameTable.js:3433-3435). The table broadcasts a normal `tableState` (all-in runout active, no current actor) (ENG/createWsServer.js:1787-1789; flag `allInRunoutActive` ENG/flopGameTable.js:4270).
2. The engine creates the vote: a map `playerId -> null` for each involved player and a deadline `endsAt = now + RIT_CONSENT_MS` (ENG/createWsServer.js:1790-1795). `RIT_CONSENT_MS` = env `RIT_CONSENT_MS`, minimum 1000, **default 15000 ms** (ENG/createWsServer.js:433).
3. `run_it_twice_offer` is sent **only to the socket of each involved player** that is registered for that table (ENG/createWsServer.js:1798-1800; socket resolution ENG/createWsServer.js:257-273). A player with no open socket simply does not receive it; the vote still waits for the deadline.
4. If a player reconnects/refreshes and sends `getState` inside the window **before answering**, the offer is sent again with `resent:true` and the time left (ENG/createWsServer.js:3245-3251, 699-705). A player who already answered never gets it again.
5. When the vote closes, `run_it_twice_decision` goes to each involved player and `run_it_twice_outcome` goes to the **whole table** including spectators (ENG/createWsServer.js:510-520, 3093-3100, 1810-1818).

## 2. Bot-visible protocol

### 2.1 Transport [V]
Game WebSocket of the engine (not the hub, not an HTTP decision API). Connection data come from the hub: `GET /integrations/clients/tables/:tableId/connection` gives `wsUrl`, `engineAuthMode`, `engineThumbKey` (HUB/routes/integrationClients.ts:156-221); a short-lived token from `POST /integrations/clients/tables/:tableId/ws-token` (HUB/routes/integrationClients.ts:233-243). URL = `wsUrl?thumbKey=<engineThumbKey>&token=<wsToken>` (CLI/accountSession.ts:224-264). The first message must be `register {playerId, tableId}` (ENG/createWsServer.js:2437-2443, 2522-2543); the engine replies `registered` (ENG/createWsServer.js:2693-2704). Messages are JSON text frames, one object per frame, discriminated by `type` (ENG/createWsServer.js:2429-2434).

### 2.2 Inbound: `run_it_twice_offer` [V] ENG/createWsServer.js:682-691
```json
{ "type": "run_it_twice_offer", "tableId": "<table id>", "handSeq": 42,
  "timeoutMs": 15000, "totalMs": 15000 }
```
- `tableId`, `handSeq`: the hand identifiers. There is **no separate hand id**: a hand is `(tableId, handSeq)`; `handSeq` is an integer that increases by 1 per hand on that table (ENG/flopGameTable.js:2498). The same pair is in `tableState.state.tableId/handSeq` (ENG/flopGameTable.js:4245, 4279) and in `handEnded.summary.handSeq`.
- `timeoutMs`: time **left** on the server deadline when the message was built. For the first offer it equals `totalMs`; on a re-send (`"resent": true`) it is smaller. `totalMs`: full window. Compute your own deadline as `receivedAt + timeoutMs` and answer well before it.
- Optional `resent:true` on a re-send. Treat unknown fields as ignorable.
- The offer does not state the street, the pot, or the other players: read them from the last `tableState` (`phase`, `board`, `handSeq`).

### 2.3 Outbound reply: `run_it_twice_response` [V] ENG/createWsServer.js:3042-3104
```json
{ "type": "run_it_twice_response", "tableId": "<table id>", "handSeq": 42, "accepted": true }
```
- `accepted`: boolean. **Only the literal `true` accepts**; anything else (false, missing, string) is a decline (ENG/createWsServer.js:3084).
- `handSeq`: optional but strongly recommended; if present and different from the open vote the reply is refused `STALE_HAND` (ENG/createWsServer.js:3067-3076).
- `tableId`: optional on a table-bound socket (the registered table is used, ENG/createWsServer.js:2898-2902); required if the socket is not bound to a single table.
- No player id is sent: the player is the one the socket registered as.
- Answer once. A second answer from the same player changes nothing and returns the recorded one with `duplicate:true` (ENG/createWsServer.js:3079-3083).

### 2.4 Engine acknowledgement: `run_it_twice_response_ack` (to the sender only) [V]
| Case | Message | Source |
|---|---|---|
| Recorded | `{"type":"run_it_twice_response_ack","ok":true,"accepted":true}` | ENG/createWsServer.js:3086 |
| Duplicate | `{"type":"run_it_twice_response_ack","ok":true,"accepted":<recorded>,"duplicate":true}` | ENG/createWsServer.js:3081 |
| No vote open (late, or RIT off, or window closed) | `{"ok":false,"reason":"NO_PENDING_RIT"}` | ENG/createWsServer.js:3045-3053 |
| Not in the hand | `{"ok":false,"reason":"NOT_INVOLVED"}` | ENG/createWsServer.js:3055-3064 |
| Reply for another hand | `{"ok":false,"reason":"STALE_HAND"}` | ENG/createWsServer.js:3067-3076 |

An `ok:true` ack means "your answer is recorded", **not** "two boards".

### 2.5 Decision, deadline and default [V]
- The vote closes as soon as **any player declines** or **all have answered** (ENG/createWsServer.js:3088-3102). Two boards only if everyone accepted.
- `run_it_twice_decision` to each involved player: `{"type":"run_it_twice_decision","accepted":true|false,"tableId":"..."}` (ENG/createWsServer.js:3094-3098). It carries **no** `handSeq`. Then `run_it_twice_outcome` to the whole table: `{"type":"run_it_twice_outcome","tableId","handSeq","accepted"}` plus `reason:"timeout"` only on timeout (ENG/createWsServer.js:510-520).
- **Deadline / default on no answer:** after `RIT_CONSENT_MS` (default 15 s) without every answer the engine sends `run_it_twice_decision {accepted:false, reason:"timeout"}` to all involved, `run_it_twice_outcome {accepted:false, reason:"timeout"}` to the table, and **runs the board once** (ENG/createWsServer.js:1802-1828). A single silent bot is enough to cost every player at the table the full wait.
- On accept: the engine waits the runout delay (default 800 ms, env `STREET_RUNOUT_DELAY_MS`, ENG/createWsServer.js:366-368, 1838-1848), deals both runouts and settles (ENG/flopGameTable.js:3138-3187).

### 2.6 Recognising the final result (two boards) [V]
1. A `tableState` is broadcast with both boards: `state.board` (board A), `state.boardB` (board B) and `state.runItTwice = {sharedCards: N}` (ENG/flopGameTable.js:4252-4253; broadcast ENG/createWsServer.js:1914).
2. After the showdown display delay (default 2500 ms, ENG/createWsServer.js:345, 1963-1972) the engine sends `handEnded` (ENG/createWsServer.js:638-659):
   - top level: `type:"handEnded"`, `tableId`, `at`, `reason:"showdown"`, `showdown:true`, **`runItTwice:true`**, `winners:[playerId,...]`, `summary`;
   - `summary.runItTwice = {sharedCards: 0|3|4}` (0 = all-in preflop, 3 = on the flop, 4 = on the turn) (ENG/flopGameTable.js:3735);
   - `summary.board` (A) and `summary.boardB` (B), 5 cards each, cards like `"As"`, `"Td"` (ENG/flopGameTable.js:3724-3726);
   - `summary.pots`: for **each** pot (main, side...) **two consecutive entries**: the first is board A's half, the second board B's half (the odd cent goes to B), each with `winnerPlayerIds` (ENG/flopGameTable.js:3681-3695);
   - `summary.showdownHands` = rank of each player on board A, `summary.showdownHandsBoardB` = on board B (ENG/flopGameTable.js:3714-3742).
3. Discriminator: use `handEnded.runItTwice === true` (or `summary.runItTwice`). Do **not** use `boardB != null` alone: a table configured as double-board also fills `boardB`, but never has `runItTwice` (and never an RIT offer).
4. A hand where the vote failed ends with a normal `handEnded` (no `runItTwice`, `summary.boardB` absent). A `handEnded` may be sent again with `replay:true` after a `getState` (ENG/createWsServer.js:666-675): dedupe on `(tableId, summary.handSeq)`.

## 3. Worked example (fictitious values, no credentials)

Heads-up NLH, table `tbl-demo-01`, players `botA` (hero) and `vilB`, hand 42. Both all-in on the flop `As 7d 2c`. Window 15 s.

```text
-> {"type":"register","playerId":"botA","tableId":"tbl-demo-01"}
<- {"type":"registered","playerId":"botA","defaultStack":...,"subscribedTables":["tbl-demo-01"]}
... hand 42 is played; botA and vilB are all-in on the flop ...
<- {"type":"tableState","tableId":"tbl-demo-01","state":{"phase":"flop","board":["As","7d","2c"],"boardB":null,"runItTwice":null,"allInRunoutActive":true,"currentActorSeat":null,"handSeq":42, ...}}

1. OFFER (to botA and vilB only)
<- {"type":"run_it_twice_offer","tableId":"tbl-demo-01","handSeq":42,"timeoutMs":15000,"totalMs":15000}

2. BOT REPLY (accept)
-> {"type":"run_it_twice_response","tableId":"tbl-demo-01","handSeq":42,"accepted":true}
<- {"type":"run_it_twice_response_ack","ok":true,"accepted":true}

3. vilB also accepts -> confirmation
<- {"type":"run_it_twice_decision","accepted":true,"tableId":"tbl-demo-01"}
<- {"type":"run_it_twice_outcome","tableId":"tbl-demo-01","handSeq":42,"accepted":true}

4. RESULT (about 0.8 s + 2.5 s later at default pacing)
<- {"type":"tableState","tableId":"tbl-demo-01","state":{"phase":"showdown","board":["As","7d","2c","Kh","9s"],"boardB":["As","7d","2c","4c","Jd"],"runItTwice":{"sharedCards":3},"handSeq":42, ...}}
<- {"type":"handEnded","tableId":"tbl-demo-01","at":1789000000000,"reason":"showdown","showdown":true,"runItTwice":true,"winners":["botA","vilB"],
    "summary":{"schema":2,"handSeq":42,"finalPotChips":200,
      "board":["As","7d","2c","Kh","9s"],"boardB":["As","7d","2c","4c","Jd"],
      "runItTwice":{"sharedCards":3},
      "pots":[{"amount":100,"winnerPlayerIds":["botA"]},{"amount":100,"winnerPlayerIds":["vilB"]}],
      "showdownHands":[{"playerId":"botA","name":"Pair","descr":"Pair, A's","isWinner":true,"holeCards":["Ah","8h"]},
                       {"playerId":"vilB","name":"Pair","descr":"Pair, 7's","isWinner":false,"holeCards":["7s","Qc"]}],
      "showdownHandsBoardB":[{"playerId":"botA","name":"Pair","descr":"Pair, A's","isWinner":false,"holeCards":["Ah","8h"]},
                             {"playerId":"vilB","name":"Two Pair","descr":"Two Pair, 7's & 4's","isWinner":true,"holeCards":["7s","Qc"]}],
      "startingStacks":{...},"finalStacks":{...}, ...}}
```
(Structure follows the real engine fixture `services/player-frontend/lib/poker/__fixtures__/engine-ante-rit-excess-review-r2.json` and the tests `wsRitConsent.test.js:109-134`; stacks, ids and timestamps here are invented. Field names `name`/`descr` of the ranks come from the engine; the exact rank strings may vary.)

### 3.1 How a DECLINE looks
Any involved player sends `accepted:false` (or the bot sends false):
```text
-> {"type":"run_it_twice_response","tableId":"tbl-demo-01","handSeq":42,"accepted":false}
<- {"type":"run_it_twice_response_ack","ok":true,"accepted":false}
<- {"type":"run_it_twice_decision","accepted":false,"tableId":"tbl-demo-01"}      (to all involved, no reason field)
<- {"type":"run_it_twice_outcome","tableId":"tbl-demo-01","handSeq":42,"accepted":false}
... one board is dealt ... then a normal handEnded: runItTwice absent, summary.boardB absent
```
The decision is sent at once, even if other players had not answered yet. A later answer from them gets `ack {ok:false, reason:"NO_PENDING_RIT"}` (test `wsRitConsent.test.js:136-157`).

### 3.2 How a TIMEOUT looks (what happens today if the bot never replies)
```text
<- {"type":"run_it_twice_offer", ... "timeoutMs":15000,"totalMs":15000}
   ... 15 s, no (or not all) responses ...
<- {"type":"run_it_twice_decision","accepted":false,"reason":"timeout","tableId":"tbl-demo-01"}
<- {"type":"run_it_twice_outcome","tableId":"tbl-demo-01","handSeq":42,"accepted":false,"reason":"timeout"}
... one board, normal handEnded ...
-> (a reply sent now) {"type":"run_it_twice_response", ...}  ==>  <- {"type":"run_it_twice_response_ack","ok":false,"reason":"NO_PENDING_RIT"}
```
`reason:"timeout"` appears only on the timeout path (ENG/createWsServer.js:1812-1818); a normal decline has no `reason`.

## 4. Human client path vs the bot path

### 4.1 Human client (web) [V]
The player web client reads `run_it_twice_offer` from the same game WebSocket (services/player-frontend/lib/ws/useEngineSocket.ts:904-909, parser lib/types/engineEvents.ts:50-62), shows a dialog with the countdown and sends `run_it_twice_response` with `handSeq` and `accepted` (useEngineSocket.ts:2216-2232). It is exactly the contract above: same engine, same socket type. There is no separate human-only channel.

### 4.2 Bot path [V for our code; N for PokerSuite]
- **The offer goes only over the game WebSocket**, to the socket registered for that player and table. No HTTP endpoint exposes the offer or accepts the answer. [V]
- **Our reference client** (`CLI/tableActor.ts`, packaged as `bluffpoker-connector`): handles `registered, tableState, actionResult, playerAction, playerAutoFolded, handEnded, sitInResult, leaveTableResult, leave_rejected, seatVacated, error` and `default: return` for everything else (CLI/tableActor.ts:622-668). `run_it_twice_offer` only appears in a message-name catalogue for diagnostics (CLI/diagnostics.ts:76). It never sends `run_it_twice_response`; its outbound messages are `register, getState, sitIn, action, playerLeaveTable, ping` (connector README; CLI/tableActor.ts:453, 626, 1116). Our own bot handoff states the same: bots ignore RIT messages, and RIT must be OFF on bot tables (BOTS handoff 2026-10-15). [V]
- **Our old decision service integration (PDecision `/api/decision`, `/api/health`, `/api/handhistory`)** has no consent action: the decision API only returns a poker action for a turn (fold/check/call/bet/raise); our adapter ran on top of the same WebSocket client above. The vendor's notes we hold say "run it twice/three times supported (normal strategy, recorded winnings not exact)" for hand history/strategy - that says nothing about answering the consent. [V for what we hold; the guide itself is Nenad's]
- **PokerSuite agent (installed 2026-11-07 on the bot VPS):** we do not have its code or logs. Whether it (a) is built on our connector (then it ignores the offer), (b) has its own WebSocket handler that answers `run_it_twice_offer`, or (c) cannot send a reply, is **not known to us**. [N]

### 4.3 Verified facts vs. things only Nenad can confirm
| Verified by BluffPoker source | Only Nenad can confirm |
|---|---|
| Offer is `run_it_twice_offer` on the game WS, to involved players only | Does the agent's socket layer deliver unknown `type`s to a handler, or drop them? |
| Reply is `run_it_twice_response {tableId, handSeq, accepted}`; ack is `run_it_twice_response_ack` | Does the agent have an action/command that sends this frame? Can it be configured per bot (always accept / always decline / never)? |
| Default window 15 s (field `timeoutMs`), default on no answer = one board | Which tables with RIT on his bots actually sit at (the setting is ours, per table) |
| Our reference client ignores the offer | Whether his agent is built on that client |
| Two-board result = `handEnded.runItTwice:true`, `summary.boardB`, pots in A/B pairs | Whether his result parser reads `boardB` / pairs of pots, or only `winners` (a RIT hand pays two boards: winners may be two different players) |
| RIT never offered on double-board tables, on the river, at-the-deal hands, PLO5 8-handed preflop | Whether his hand-history upload handles two boards [N] |

## 5. Three checks for Nenad (in his own agent logs)

1. **Offer received?** Search the agent's WebSocket log for the raw frame `"type":"run_it_twice_offer"` on the bot's table around the all-in. Record `tableId`, `handSeq`, `timeoutMs`, receive time. No frame = the agent's socket layer filters it, the bot was not involved (folded) or the table has RIT off (the table option is ours: tell us the table id and we confirm the flag). Also check the bot was involved: it must be non-folded when betting closed.
2. **Reply sent before the deadline?** Search for an outbound frame `"type":"run_it_twice_response"` with the same `handSeq`, `accepted:true|false` and a send time earlier than `receive time + timeoutMs`. No outbound frame = the agent has no handler (the engine will log `reason:"timeout"` for that hand). If sent, check it went on the same socket/table.
3. **Engine accepted it?** Search for the inbound `run_it_twice_response_ack`: `ok:true` with your `accepted` value = recorded; `ok:false` with `NO_PENDING_RIT` = too late (deadline passed or vote already closed by another decline), `STALE_HAND` = wrong `handSeq`, `NOT_INVOLVED` = the player was not in the hand. Then the closing messages: `run_it_twice_decision` (`accepted`, and `reason:"timeout"` if it timed out), `run_it_twice_outcome`, and `handEnded` with `runItTwice:true`.

If any hand shows `reason:"timeout"` for a bot that was supposed to answer, send us: table id, `handSeq`, UTC time of the offer and of any reply, so we can match it with the engine log.

## 6. Recommended safe default for bot tables
Until the agent is confirmed to answer: keep `runItMultiTimeEnabled` OFF on tables where his bots sit (this is the setting our own load scripts use, `RUN/scripts/pdecision-load.ts:217`, per our evaluation notes), or, if it is ON, make the bot send an explicit `accepted:false` (or `true`) immediately on receipt of the offer, so a hand never waits the full window.
