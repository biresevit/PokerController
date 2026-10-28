# Verification — October 25, 2026

- Offline suite: **24 passed**, live integration test skipped by default.
- Opt-in live lifecycle test: **1 passed** against the actual demo.
- Endpoint: `wss://web.evenbetpoker.com/front`.
- Account: `monica`, player ID `73582719`.
- Lobby discovery returned 123 tables during the initial probe.
- Table `113891676`: joined, read limits (80–200 chips), reserved a seat,
  bought in with 80 chips, verified the seat/stack, and left.
- Table `128048287` (`Call Time 30min`): bought in with 80 chips, received
  table updates, and completed six REST-driven PDecision actions across five
  hands. One hand reached the flop. PDecision returned a check and five folds;
  the server accepted all six actions. No protocol errors were observed.
- The controller left the table at the end of each test. No controller bot was
  left playing.
- PDecision was ready locally at `http://127.0.0.1:5005` (running binary version
  `1.0.0.22`). Started for testing with Azure hand-history uploads and background
  history downloads disabled, and artificial decision delays disabled, via
  process environment variables. No PDecision source/configuration files or
  database records were changed by this implementation.

Redacted server captures and console output are in ignored `artifacts/`:
`seat.jsonl`, `play-v2.jsonl`, and `play-v2.log`. The browser was used once to
inspect the current public client protocol; it isn't needed by PokerController.

Live coverage does not yet include a PDecision-generated call, bet, or raise,
Omaha, or reconnecting during an active hand. Those action amounts and legality
checks have offline coverage. The controller stops on connection failure rather
than automatically replaying an action whose outcome is unknown. Tournament
flows and exact split-pot/side-pot payout reconstruction remain outside this port.

## Evenbet hand-history upload (2026-10-26)

- This PC's Azure `dbo.CustomerMachine` registration was changed from
  BluffPoker / RunYourOwnPokerSite to Evenbet / EvenBet, retaining the internal
  customer's machine and active status.
- PDecision `1.0.0.24` is running on `http://127.0.0.1:5005` with Azure hand-history
  upload enabled. Its health response reports Evenbet / EvenBet for both machine
  and hand-history destination.
- A 10-minute live test on NLH table `128108711` sent five completed Monica
  histories. Azure `dbo.CustomerHandHistory` contains hand IDs `162968555`,
  `162968560`, `162968567`, `162968579`, and `162968599` under customer 1 and
  `1/Evenbet/<handId>.txt`; the PDecision log confirms uploads.
- The fifth hand used the new `/api/handhistory/complete` endpoint, which saves
  the final hand without waiting for another hand. Its live settlement frame
  was rejected by the ordinary frame validator, but explicit completion used
  the verified payout and wrote the final local history and Azure entry.
- Monica left the table. A fresh table-state check found no Monica seat.
- Offline suite: 51 passed, 1 opt-in live test skipped.

## Mid-hand roster regression (2026-10-25)

Hand 162963359 exposed a controller bug: Kristiny joined during the turn decision,
invalidating the raw-seat fingerprint. The controller discarded PDecision's valid
BET 10 and sent a second request with the newcomer's stack. An isolated REST replay
of the four saved requests reproduced Action 6 on that fourth request.

The mapper now freezes the dealt-player roster per hand. Nonparticipants have
empty names, zero stacks/bets, EmptySeats=true and InGame=false. Departed players
retain their identity and last known stack/bet with InGame=false; their retained
bet clears on the next street. The next hand rebuilds the roster. Away status
alone does not imply a fold while the server still shows cards.

Decision fingerprints use this hand roster and ignore seat status flags. A replay
test of the three captured turn snapshots verifies that BET 10 produces exactly
one Play(action=8, funds=1000), without a second decision request. These regression
tests send no live game actions. Further tests cover departures, replacements,
sit-outs, next-hand admission and invalidation when the actor changes.

## Mixed multi-table run (2026-10-25)

Monica joined five tables over one connection: one PLO4 and four NLH. A sixth
candidate had no free seat and was skipped. A 30-minute run produced 142 accepted
actions: 53 PLO4 and 89 NLH. One NLH table was left early after PDecision returned
CHECK while EvenBet allowed only fold/call/raise. The four remaining tables played
until the deadline and were left. A subsequent join verified Monica had no seat
at any of the five tables. The redacted capture and logs are under
`artifacts/multi-live-20261025-194738.*`.

The illegal-check case now maps to a free fold in source; this change was made
after the run started and has offline test coverage. The live process did not
use that newer mapping. The runner logs table-specific failures and keeps other
tables active. The PLO4 table produced accepted preflop, flop, turn, and river
decisions. The PDecision server's solver-backed responses included calls, bets,
raises, checks, and folds.

## BluffPoker profile import (2026-10-25)

`NICKNAME-PROFILE-500-20261025.csv` contained 500 unique nicknames and eight
known profile labels. The repeatable `python -m poker_controller.import_profiles`
command imported all 500 into Azure pokerdb's existing `dbo.BotAccount` for
BluffPoker. It updates only Strategy for a nickname already present and leaves
stake limits and active status unchanged. A repeat dry run reported zero inserts,
zero updates, and 500 unchanged rows. PDecision's read-only
`BotAccount_ListForCasino` procedure returned 520 active accounts afterward:
the 500 imported names plus 20 pre-existing test accounts. Profile mapping
follows `StrategyUtil.GetProfileForID(1..8)`; the import uses the
`SqlConnectionString` environment variable without recording credentials.
