# Expanded Formal Markets

Treat all supported markets as one candidate pool. Asian handicap and full-time totals have no automatic priority over the markets below. Select at most one primary from the whole pool; archive other threshold-qualified directions as secondary references only. Never settle, grade, stake, or include a secondary in accuracy or ROI.

“Supported” does not mean “currently formal-enabled.” Under
`strict-oos-market-policy-v1`, corner totals and corner handicaps are
`observation_only`. The current corner manager binds historical training and walk-forward
artifacts but deliberately emits `formal_corner_total_eligible=false` and
`formal_corner_handicap_eligible=false`. Until a later manager version binds independent
strict live-forward validation and turns on the matching flag, a corner candidate may be
shown only as `◇` observation and cannot be the primary or a settled secondary.

## Supported markets

| Market key | Display example | Model basis | Required current market |
|---|---|---|---|
| `goal_range` | 总进球 2-3球 / 7+球 | Full football score matrix | Every mutually exclusive goal-band outcome |
| `btts` | 双方进球：是 / 否 | Full football score matrix | Both Yes and No |
| `corner_total` | 角球大 10.5 | Independent total-corners distribution | Both over and under at the selected line |
| `corner_handicap` | 主队角球 -1.5 | Independent corner-margin distribution | Both home and away at the selected line |

Use `goal_range_pick` and `btts_pick` for policy-enabled formal records. The
`corner_total_pick` and `corner_handicap_pick` fields are reserved for a future policy version
or quarantined legacy compatibility; do not populate them while the corresponding manager
formal flag is false. Give each admitted formal pick a role; exactly one formal pick must be
`primary` and every other formal pick must be `secondary`.

## Probabilities

Use the same complete score matrix as exact-score and total-goals analysis:

- `P(goal range a-b) = Σ P(H=h,A=a)` for every cell whose `a <= h+a <= b`.
- `P(n+) = Σ P(H=h,A=a)` for every cell whose total is at least `n`.
- `P(BTTS yes) = Σ P(H=h,A=a)` where `h >= 1` and `a >= 1`.
- `P(BTTS no) = 1 - P(BTTS yes)`.

Do not create a new goal distribution only to favor a market. Preserve 0-0 and the distribution tail when calculating every band, including `7+`.

Build corner probabilities separately with the registered corner-count model. Its training
data must contain verified 90-minute home and away corner counts, chronological fixture
times, normalized league/team identities, dataset hash, and an as-of cutoff. Derive both the
total-corners and home-minus-away distributions from that one registered joint prediction.
Width/crossing, dangerous attacks, set-piece volume, match-state tendencies, and confirmed
personnel are independent evidence or sensitivity inputs unless a later versioned model
actually learns them; do not hand-adjust the probability artifact from prose. Football
goals, possession, or xG alone are not corner evidence.

## Prices and evidence

- Use real current odds from the named market. Never reuse totals odds for a goal range, exact-score odds for BTTS, or football handicap odds for a corner market.
- Remove margin only from a complete mutually exclusive outcome set. If any outcome is missing, show the model probability as an observation and do not calculate a formal edge.
- Record source, timezone-aware collection time, bookmaker count, market completeness, odds format, `consensus` or `median` price basis, model probability, no-vig market probability, edge, EV, and market signal.
- Require strictly positive EV and edge plus medium/high data quality for an ordinary formal direction. EV `8%` and edge `4pp` remain adverse-signal qualification thresholds, not generic targets or evidence to tune toward.
- Require chance-quality evidence or an attacking configuration supported by confirmed lineups for goal ranges and BTTS.
- For diagnostic corner qualification, require at least three firms plus
  `corner_profile_evidence`. This still cannot override a false manager formal flag.
- When line and related markets move materially against or materially conflict with a selection, require EV `>= 8%`, edge `>= 4pp`, at least five firms, and independent confirmed-lineup or fundamental evidence.

Compare candidates using an executable consensus or median price, edge, evidence quality, and market depth. Do not rank a single-firm outlier price as the best candidate merely because its raw EV is highest.

## Archive and lineup check

The audit contract applies to the original markets as well as the expanded markets. Asian
handicap, full-time total, and first-half Asian/total picks must include explicit odds
format, complete-market flag, selected no-vig market probability, source, timezone-aware
collection time, consensus/median price basis, bookmaker count, and five settlement
probabilities. The stored `loss` key means full loss. HT/FT requires all nine current
outcome probabilities from the same complete market. The archive command recalculates EV
and edge and rejects caller values that disagree.

Pass every outcome price with repeated `--<prefix>-market-odds LABEL:PRICE` values. A
total needs both `--total-market-odds over:1.95` and
`--total-market-odds under:1.95`; BTTS needs `yes` and `no`; Asian/corner handicap needs
`home` and `away`; first-half 1X2 needs `home`, `draw`, and `away`; HT/FT needs all nine
`HH` through `AA`. A goal-range set must start at zero, contain continuous non-overlapping
bands, and end in `N+`, such as `0-1`, `2-3`, `4-6`, `7+`. The selected complete-market
price must equal the executable pick price. The archive command converts every outcome
using the declared odds format and calculates the no-vig probabilities itself.

Store fixed, explicit fields:

- Goal range: `selection`, `minimum_goals`, `maximum_goals`, `odds`, `odds_format`, `probability`, `market_probability`, `ev`, `edge_pp`, `firm_count`, `market_complete`, `market_source`, `market_collected_at`, `price_basis`, `market_signal`, `role`.
- BTTS: `side` (`yes` or `no`) plus the common price, probability, evidence, and role fields.
- Corner total: `side` (`over` or `under`), `line`, the common fields, and a settlement-probability distribution.
- Corner handicap: `side` (`home` or `away`), `line`, the common fields, and a settlement-probability distribution.

Explicitly set `odds_format` to `decimal` or `hong_kong`. Recalculate no-push EV as `p * decimal_odds - 1` or `p * (1 + hong_kong_odds) - 1`; reject a supplied EV that differs. For every split-settlement market, persist `full_win`, `half_win`, `push`, `half_loss`, and `loss` probabilities, require them to sum to one, and calculate EV with the actual split settlement. Set impossible states to zero: half-lines allow only full win/loss, integer lines allow full win/push/loss, and quarter lines allow full/half win and full/half loss but no push. Preserve legacy records with no format under their historic behavior in a quarantined cohort; never rewrite old reviewed ROI or describe it as strict forward performance.

Use the dedicated `record` arguments:

- `--goal-range-selection`, `--goal-range-odds`, `--goal-range-odds-format`, `--goal-range-probability`, `--goal-range-market-probability`, `--goal-range-ev`, `--goal-range-edge-pp`, `--goal-range-firm-count`, `--goal-range-market-source`, `--goal-range-market-collected-at`, `--goal-range-price-basis`, `--goal-range-market-signal`, `--goal-range-market-complete`
- Repeat `--goal-range-market-odds LABEL:PRICE` for every complete outcome. Use the equivalent repeated `--*-market-odds` flag for each other formal market.
- The equivalent `--btts-*`, `--corner-total-*`, or `--corner-handicap-*` arguments for those markets
- For each corner market, also pass the five `--*-full-win-probability`, `--*-half-win-probability`, `--*-push-probability`, `--*-half-loss-probability`, and `--*-loss-probability` values
- `--corner-profile-evidence` whenever a future policy-enabled formal corner pick is archived

Pass the relevant `--*-market-complete` flag only after every required outcome of that current market has been verified. While the manager formal flags remain false, keep these values in a corner-ranker observation artifact and do not pass formal corner-pick arguments to `memory_store.py record`.
The archive retains the complete outcome prices, raw implied probabilities, server-calculated no-vig probabilities, and audit metadata. The validator checks `edge_pp = 100 * (model_probability - market_probability)`.

A lineup check may maintain, replace, or cancel any market. Compare goal-range identity by its range, BTTS identity by its side, and corner identity by side plus line. Recalculate the versioned selection policy at current prices: when the old thesis remains eligible, replacement needs a current confidence gain of at least five points; when hard information invalidates it, archive that reason and use the new safe Rank 1 or no primary. Preserve the previous version append-only in `revisions` and settle only the final active version.

## Settlement

- Goal range: use the verified regulation-time total goals; range bounds are inclusive.
- BTTS: `yes` wins only when both teams score in regulation time.
- Corner markets: use official 90-minute corners including stoppage time, excluding extra time and shootouts unless explicitly stated otherwise.
- Settle corner quarter lines using full/half win, push, and full/half loss rules.
- If the final active primary is a corner market and either official corner count is unavailable, reject review settlement and leave the record pending for later completion.
- Convert decimal winning odds to net profit with `odds - 1`; Hong Kong odds are already net profit. Exact scores remain diagnostics only.
