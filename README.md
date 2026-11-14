# PokerController | Protocol adapters and workers

An asynchronous Python worker connecting poker platforms to a local decision service. It translates protocol messages into shared table state, coordinates multiple tables, and executes scheduler commands.

## Engineering highlights

- WebSocket and REST adapters for EvenBet and BluffPoker with a shared state and decision pipeline.
- Multi-table execution, seat reconciliation, guarded retries, payment idempotency, and restart recovery.
- Operational telemetry, manual controls, table chat, and a companion Windows updater.

**Technology:** Python 3.11, asyncio, WebSockets, HTTPX, SQL/ODBC, pytest, C# updater.

## Platform architecture

```text
PokerBackoffice -> Azure SQL <- ScheduleController (Azure Functions)
                                     | commands / telemetry
                                PokerController
                                     | REST
                                  PDecision
                                     |
                         simulator / DecisionMaking
```

[PokerBackoffice](https://github.com/biresevit/PokerBackoffice) | [ScheduleController](https://github.com/biresevit/ScheduleController) | [PokerController](https://github.com/biresevit/PokerController) | [PDecision](https://github.com/biresevit/PDecision-portfolio) | [simulator](https://github.com/biresevit/simulator-portfolio)

## Setup and operations

A full deployment requires configured infrastructure, platform accounts, and companion components. The following sections describe setup, validation, and operational requirements.

**Deploying:** see `../PokerBackoffice/docs/deploy.md`, section 7 (PC release).

Standalone Python EvenBet controller. It uses WebSockets for the poker server and
REST for PDecision. It has no HopperV2, .NET, SQL, Azure, or browser dependency.

Defaults are the supplied demo account (`monica` / `poker`),
`wss://web.evenbetpoker.com/front`, Origin `https://demo.evenbetpoker.com`, skin
`evenbetpoker`. Credentials can be overridden with `EVENBET_USERNAME` and
`EVENBET_PASSWORD` or command options. No credentials or session tokens are logged.

## Setup

```powershell
cd C:\Users\bires\projects\bot\PokerController
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest -q
```

## Use

```powershell
# Log in and list the current lobby tables, occupancy, and minimum buy-ins.
.\.venv\Scripts\python.exe -m poker_controller tables

# Subscribe to a table without taking a seat; write redacted protocol messages.
.\.venv\Scripts\python.exe -m poker_controller observe --table 113891676 --seconds 30 --capture artifacts\observe.jsonl

# Reserve a free seat, buy in with 80 chips, confirm the stack, and leave.
.\.venv\Scripts\python.exe -m poker_controller seat --table 113891676 --buy-in 80

# Keep a seat deliberately after the command ends, or leave an existing seat.
.\.venv\Scripts\python.exe -m poker_controller seat --table 113891676 --buy-in 80 --keep-seat
.\.venv\Scripts\python.exe -m poker_controller leave --table 113891676

# PDecision must already report ready at this address.
.\.venv\Scripts\python.exe -m poker_controller health
.\.venv\Scripts\python.exe -m poker_controller play --table 128048287 --buy-in 80 --seconds 240 --max-actions 6 --capture artifacts\play.jsonl

# One login can play several NLH/PLO4 tables for 30 minutes, then leave all of them.
.\.venv\Scripts\python.exe -m poker_controller play-multi --tables 128197713 128048285 128016534 --buy-in 80 --seconds 1800

# Preview a repeatable nickname,profile import for an existing customer/casino.
.\.venv\Scripts\python.exe -m poker_controller.import_profiles C:\Users\bires\Downloads\NICKNAME-PROFILE-500-20261025.csv

# After reviewing the counts, commit it. Requires SqlConnectionString in the environment.
.\.venv\Scripts\python.exe -m poker_controller.import_profiles C:\Users\bires\Downloads\NICKNAME-PROFILE-500-20261025.csv --apply
```

### BluffPoker

BluffPoker (enPlatform `BluffPokerPlatform` = 18) is played over its hub REST API and one engine WebSocket
per table (`bluffpoker.py`, ported from BluffPoker's player connector). `BluffPokerClient` has the same
interface as `EvenBetClient` and translates each table state into the EvenBet snapshot shape, so the bot,
HandState mapping, manual control, rebuys, breaks and hand histories are shared.

Credentials stay on the PC in `C:\PokerSuiteluffpoker.json` (or the file named by `BLUFFPOKER_CONFIG`),
never in the database or the repository:

```json
{"hub_url": "https://hub.bluffpoker.club", "api_key": "<client API key>", "origin": "https://bluffpoker.club",
 "accounts": {"guido66": {"email": "<login>", "password": "<password>"}}}
```

`BP_HUB_BASE_URL`, `BP_CLIENT_API_KEY`, `BP_ORIGIN` and `BP_CLIENT_IP` override the file; a scheduler site's
`EndpointUrl` overrides the hub URL.

```powershell
python -m poker_controller tables --site bluffpoker --username guido66
python -m poker_controller play --site bluffpoker --username guido66 --table <table id> --buy-in 100 --seconds 300
```

The hub allows 30 logins per 15 minutes per IP and 10 failed logins per account; sessions reuse their token
(refreshed before it expires). A rejoin refused with `REJOIN_MIN_NOT_MET` is repeated once with exactly the
stack the bot left with (`rejoin.required`). Seat payments carry an idempotency key, so an unconfirmed payment
is asked again with the same key and never paid twice; game actions are never resent. Tournaments, chat and
showing cards are not supported on BluffPoker yet.

### Azure-scheduled worker

The Azure Function plans commands every 30 seconds. This PC only runs PDecision
and the PokerController worker; it does not run a local scheduler. Start the
worker after PDecision is healthy on port 5005:

```powershell
cd C:\Users\bires\projects\bot\PokerController
$env:SqlConnectionString = '<pokerdb connection string>'
.\.venv\Scripts\python.exe -m poker_controller.worker --output artifacts\azure-worker
```

The worker runs continuously. Create an empty `stop` file inside the output
directory for a graceful stop. It logs in only for a due join task (or a
short lobby scan), logs out after that account's final table closes, publishes
machine/lobby/live-view telemetry, and never calculates the schedule locally.
Machine capacity is configured in the backoffice/database and enforced only by
the Azure scheduler; it is deliberately not a PokerController command option.

Use `--decision-url` and `PDECISION_API_KEY` for another PDecision instance. The
controller checks health before taking a seat. PDecision retains its own existing
machine registration, account/profile, and strategy configuration; this project
does not change that database. The current PDecision service uses the platform
and casino from its machine registration.

The profile importer uses `pyodbc` (`pip install -e ".[db]"`). It validates the
header, unique nicknames and eight supported profile labels; checks that the
customer has an active machine at the casino; and applies inserts or profile-only
updates in one transaction. It preserves existing stake limits and active status.
The importer writes to the existing `dbo.BotAccount` table, keyed by casino and
nickname. `--customer` checks that the named customer runs that casino; account
rows themselves are scoped by casino. Existing stake limits and active flags are
preserved when their profile changes.

The screenshot's table `113891676` may be empty. Choose a supported occupied table
from `tables` to exercise actual hands. Seating can wait until the next big blind
or hand before hole cards and actions arrive. `--seat` is zero-based. `--buy-in`
is in chips (80 means 8000 protocol funds), not big blinds.

By default, commands unsubscribe/leave the table in `finally`. In `play-multi`, a
failure at one table leaves that table while the others continue. `--keep-seat`
opts out for single-table commands. A broken network connection can prevent
cleanup; rerun `leave` after connectivity returns. Actions with ambiguous
outcomes are never retried automatically.

## Implemented flow

1. `ClientVersion` and `Login`, correlated by `srcMsgId`.
2. Lobby listing, table selection/subscription, current state, buy-in limits.
3. Free-seat selection, `ReserveSeat`, `SitDown`, stack confirmation, automatic
   blinds, reattachment to an existing seat, return from away, and table exit.
4. Hero-relative `HandState`: cards, blinds, stacks, bets, board, pot, dealer,
   dealt seats, and hand identity. Zero-valued indices omitted by EvenBet are
   decoded as zero.
5. `POST /api/decision` on the hero's turn, then `Play` with the returned action.
   A PDecision raise-to amount is converted into additional minor-unit funds,
   clamped to the legal server range. If a raise cannot reopen betting and only
   call/fold are legal, it calls. An invalid free check folds, and a PDecision
   exception takes a legal free check or fold. Other illegal responses stop that
   table's play.
6. Reject duplicate turn snapshots and decisions whose turn changed during HTTP.

Cash NLH, PLO4, and PLO5 state/action conversion is implemented. Live verification
has covered NLH and PLO4. Tournament scheduling, tournament
cashiers, unusual game variants, double boards, and bomb pots aren't ported.
Play commands buffer snapshots until EvenBet reports a final payout, then post
the complete observed hand to `/api/handhistory`. It works with `play` and
`play-multi`. Hands entered mid-game or without a final payout are omitted. The
winner amounts are inferred from the final chip increase, so split and side-pot
hands need further validation before using the resulting statistics for production.
PDecision must report `handHistoryCasino=Evenbet`, `handHistoryPlatform=EvenBet`,
and Azure hand-history upload enabled in `/api/health`; the command refuses to
send otherwise. PDecision uses this PC's machine registration for the casino and
platform. After posting the frames, PokerController calls
`/api/handhistory/complete` so PDecision saves the last hand before the table
closes. Histories are sent by default for `play` and `play-multi`; use
`--no-hand-history` to disable uploads for a local run.

## Tests

The normal suite runs offline: captured real snapshots, a local WebSocket server,
and an HTTP mock. It covers the login-to-seat lifecycle, out-of-order responses,
timeouts/disconnects, money/card conversion, seat rotation, legal raises/calls,
and stale/duplicate action prevention.

An additional live test logs into the demo, joins the requested table, buys in,
verifies the seat/stack, and leaves:

```powershell
$env:POKER_LIVE = '1'
$env:POKER_LIVE_TABLE = '113891676'
.\.venv\Scripts\python.exe -m pytest tests\test_live.py -q
```

Avoid running two live controllers with the same account simultaneously.

## Structure

| Module | Responsibility |
|---|---|
| `transport.py` | TLS WebSocket, one receive loop, request correlation, notifications, redaction |
| `client.py` | EvenBet session and table/seat/buy-in operations |
| `bluffpoker.py` | BluffPoker hub API, engine table sockets, state translation, same interface as `client.py` |
| `sites.py` | Platforms (enPlatform), client per platform, table ids |
| `state.py` | Protocol-to-HandState conversion and action amounts |
| `decision.py` | PDecision REST adapter |
| `bot.py` | Turn processing and duplicate/stale decision checks |
| `multitable.py` | One-session table dispatch, independent bot workers, per-table cleanup |
| `cli.py` | Configuration, commands, captures, cleanup |

Adapted from the `EvenbetDev` branch of the sibling `hopperv2` repository,
particularly its Evenbet login, WebSocket requests, scraper, cash cashier,
`RaiseByCalculator`, and action controller. The current frontend accepts table
requests through `/front`; no hardcoded game-slot connection is needed for the
tested flow. Future Azure configuration belongs behind a settings/account
repository, without coupling database reads to the WebSocket or state mapper.
