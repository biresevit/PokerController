# Tournament chip units

SNG and MTT table amounts use native integer tournament chips end to end:
blinds, stacks, bets, pots, payouts, HandState sent to PDecision, and returned
bet/raise targets. There is no division or multiplication by 100 on this path.

Cash tables still convert Evenbet's minor currency units to major units for
HandState and convert returned bet targets back to minor units. Tournament
buy-ins and entry fees are money, not chips; their currency conversion is unchanged.

`state.amount_scale(table)` is the shared conversion rule: 1 for SNG/MTT metadata,
100 for cash. The automatic bot uses the same scale for its HandState and the
returned decision. Calls already use the poker server's exact legal call amount;
bet/raise targets subtract the hero's existing contribution in protocol units.

Ante remains a fraction of the current big blind in the PDecision contract.
For example, a 20-chip ante with a 200-chip big blind is `Ante=0.1`, not `20`.
Blind levels come from each GameState's `bbv`/`sbv` and PDecision 1.0.0.40 updates
its stored blinds from each incoming HandState.

Live snapshots from new workers include `amount_unit='tournament_chips'`. The
website renders these directly and continues to convert older worker snapshots
that lack the marker. Deploy the backward-compatible website before the signed
worker release. Restart both managed applications via the normal rollout so
cached decisions and unfinished hand histories do not mix scales within a hand.
Historical hand histories retain the units they were recorded with.
