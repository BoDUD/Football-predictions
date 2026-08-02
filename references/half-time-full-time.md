# Half-Time and Half-Time/Full-Time Analysis

Use this guide whenever a prediction includes first-half or half-time/full-time markets.

These markets remain `observation_only` in the strict forward policy until clean live-forward
records with executable prices support a later policy version. The audited historical league
data now support a versioned nine-class model and evidence-based two-scenario selector, but
historical probability accuracy is not betting ROI and does not by itself unlock a formal
HT/FT primary.

## Required data

Collect, when available:

- Opening and current first-half 1X2, Asian handicap, and total-goals odds.
- Opening and current half-time/full-time odds for all nine outcomes.
- First-half scores from each team's recent 10 matches and comparable home/away matches.
- Goals scored and conceded in 1-15, 16-30, and 31-45+ minute bands.
- First-half 0-0 rate, first-half draw rate, first-half xG/xGA, and first-half shots/corners.
- Confirmed lineups, especially starting striker, goalkeeper, centre-back, and defensive-midfield changes.
- League first-half goal rate and the bookmaker consensus across at least five firms when possible.

Mark unavailable data explicitly. Never infer first-half or HT/FT EV from full-time odds alone.

## Probability model

1. Use `scripts/htft_model.py`, fitted separately for each competition from an audited
   `history_importer.py` dataset. The validated defaults are a 730-day half-life for the
   first-half Dixon-Coles marginal and 365 days for the full-time marginal.
2. Estimate the league's nine-cell HT/FT association from training rows only. Add Jeffreys
   smoothing of 0.5 to every cell, divide the joint by the product of its historical row and
   column marginals, and apply the resulting association lift to the fixture-specific
   first-half and full-time marginals.
3. Use iterative proportional fitting (IPF) so the final 3x3 matrix reproduces the fixture's
   half-time and full-time marginals exactly. The resulting cells are HH, HD, HA, DH, DD, DA,
   AH, AD, and AA.
4. The historical score-convolution seed is available only as an explicit experiment. It is
   not the production default because the empirical-association seed performed better in the
   fixed 2025 model-component evaluation. That cohort was later inspected during final
   selector development, so it is not untouched evidence for the end-to-end selector.
5. A complete, de-vigged, source-labelled, timestamped current first-half 1X2 market may
   replace the half-time marginal before IPF. Never use an incomplete market, an untimestamped
   workbook snapshot, or a post-kickoff price as a live anchor. The low-level model can run a
   full-time anchor experiment, but the registered production manager rejects it until the
   same anchor can be written back into the canonical full-score matrix; do not publish two
   conflicting full-time probability views.
6. Require the HT/FT full-time marginal to match the canonical full-score artifact. The HT/FT
   model supplies association structure; it does not create a conflicting full-time forecast.

Use `H`, `D`, and `A` for home, draw, and away. Examples: `DD` = half-time draw/full-time draw; `DA` = half-time draw/full-time away win.

## EV and selection rules

- Decimal odds: `EV = probability * decimal_odds - 1`.
- Hong Kong odds: `EV = probability * hk_odds - (1 - probability)`.
- Settle quarter-goal first-half lines using their real half-win/half-loss components.
- For mutually exclusive HT/FT selections sold as a two-selection ticket, calculate each leg separately. The combined hit probability is the sum of the selected outcome probabilities, but expected return depends on the stake allocated to each leg. Do not add the two EV values.
- Recommend at most one first-half direction. For HT/FT, always output exactly two probability-selected scenarios whenever a valid nine-outcome model matrix is available.
- While the strict policy keeps these markets paused, label every first-half/HTFT direction as observation even when its price and heuristic EV look positive.
- A future policy version may make first-half advice actionable only after a versioned model, current complete market, positive server-recalculated EV/edge, medium/high data quality, and lineup-time survival all pass.
- A future HT/FT formal path additionally requires a normalized supported `league_key`, a complete current nine-outcome market from at least five bookmakers, verified row/column marginals, and adequate clean live-forward calibration. Changing the display/ranking heuristic alone cannot unlock it.
- Before selection, require the matrix row sums to match the displayed first-half H/D/A probabilities and its column sums to match the displayed full-time H/D/A probabilities within 0.5 percentage points. Recalculate the matrix when either marginal check fails; do not publish or select from an inconsistent matrix.
- Diagnostic EV/edge qualification requires all nine current executable HT/FT prices from
  the same market snapshot, with source and pre-kickoff collection time. Derive the no-vig
  market probabilities internally from that complete set; a caller-supplied probability
  vector plus only some outcome prices remains incomplete and cannot qualify either scenario.
- Use `scripts/htft_ranker.py` and `probability_top2_v3_post_selection`. Select the two largest
  joint-probability cells with canonical outcome order as the deterministic tie-break. This
  replaced `scenario_stability_v2`, which reduced two-scenario hit rate from 48.48% to 45.70%
  on the fixed 2025 component cohort. Its older partial-2026 comparison mixed the Japan
  special regime and is not a formal metric. The 2025 cohort was inspected during selector
  development, so this comparison is post-selection development evidence, not an untouched
  end-to-end confirmation.
- Conditional follow-through, state continuity, full-time coherence, and exact-score result
  agreement remain visible audits but cannot replace a probability Top-2 cell. EV and price
  also cannot choose either slot.
- For a model-only matrix, treat combined Top-2 probability 0.46 only as a descriptive
  development threshold. Pass `league_key` so the ranker exposes the applicable evidence:

  | League | 2025 eligible | Covered | Hits | Hit rate | Wilson 95% lower bound |
  | --- | ---: | ---: | ---: | ---: | ---: |
  | Brazil Serie A | 380 | 125 | 72 | 57.60% | 48.84% |
  | Japan J1 | 380 | 66 | 32 | 48.48% | 36.85% |
  | Norway Eliteserien | 240 | 110 | 63 | 57.27% | 47.94% |
  | MLS | 510 | 208 | 114 | 54.81% | 48.02% |

  No league's lower bound exceeds 50%, so the ranker must not label the pair as confirmed
  confidence. Missing or unsupported `league_key` has no transferable evidence. A verified
  current half-time-market anchor likewise has no promoted pair-mass threshold; mark it
  `anchor_gate_unvalidated`. The 0.50 gate and 61.56% accuracy at 21.19% coverage apply only
  to the evaluator's untimestamped full-time-opening research proxy. Never transfer that gate
  or hit rate to a live half-time anchor. These are two-scenario coverage rates, not single-bet
  win rates or ROI.
- Positive EV and edge are diagnostic qualification gates only after the two probability scenarios are selected. While the market is paused they do not make a scenario formal; under a future enabled policy every model, market, evidence, and calibration check must also pass. Treat a high-EV outcome outside the selected pair as a market anomaly to recheck, not as an automatic recommendation.
- Show the failed threshold for every observation candidate, for example `EV -2.5%` or `市场边际仅 +1.2pp`. An observation candidate is a probability match shape, not an actionable positive-EV bet.
- If current HT/FT odds are missing, keep the same two probability-selected scenarios as `赔率缺失，不可执行`; do not invent odds, market probability, or EV. If the model matrix itself cannot be calculated, mark both slots `数据不足`.
- Do not replace the two probability-selected outputs with a generic `无正EV建议` or `观望`. Preserve the risk warning instead.

## Visual output

Add these sections after the full-time market analysis:

1. `半场判断`: first-half 1X2 probabilities, likely half-time scores, current half-time Asian/total lines, and the best positive-EV direction.
2. `半全场矩阵`: a compact 3x3 matrix for HH through AA, with row/column marginal checks and the two probability-selected scenarios highlighted.
3. `概率形态`: show exactly two rows labelled `主概率形态` and `备选概率形态`, with selection, status, joint probability, combined pair probability, confidence gate, conditional follow-through, state continuity, no-vig market probability, edge, odds, and EV. Do not show a rank column. Show the assumed stake split whenever a mutually exclusive two-selection ticket is discussed.
4. `风险`: missing odds, small samples, lineup uncertainty, and high variance.

Concise mode includes the best first-half direction plus both probability-selected HT/FT scenarios. Keep `观察候选（未达标）` labels even in concise mode.

## Supported-competition boundary

The audited 9,211-match history supports only Brazil Serie A, Japan J1, Norway
Eliteserien, and MLS. It supplies no Korean K League training or validation cohort, so never
quote these four leagues' Top-2 or gate rates for a Korean match. Use the generic analysis
path and keep Korean HT/FT output as observation until league-specific live-forward evidence
exists.

Japan J1's 180 supplied 2026 fixtures belong to a regional special format and are labelled
`competition_regime=2026_vision_regional`. The production manager trains only on
`competition_regime=regular`; it excludes those 180 rows from registered fitting and formal
evaluation metrics, retaining only their exclusion count to surface competition-regime drift.

## Calibration note from the supplied betting log

The supplied workbook contains six settled HT/FT tickets, not 206 matches. All six used half-time draw as the common branch and paired it with full-time draw/home/away. Two tickets won and four lost: a 33.3% ticket hit rate. Stakes totalled 2,000 and recorded ticket profit totalled 725 before the 9-unit rebate, a 36.25% stake-weighted ROI. The workbook's 73.4% figure is return on the initial 1,000-unit bankroll after top-up/rebate adjustments, not betting ROI.

The log does not contain per-leg odds, stake allocation, half-time score, or which branch settled as the winner. Use it to support the requested display and paired-outcome workflow only. Do not use six tickets to raise the prior probability of half-time draw or to train model weights.
