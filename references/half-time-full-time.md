# Half-Time and Half-Time/Full-Time Analysis

Use this guide whenever a prediction includes first-half or half-time/full-time markets.

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

1. Estimate separate first-half scoring rates `lambda_home_1H` and `lambda_away_1H`. Blend recent first-half production, opponent first-half concessions, home/away splits, league baseline, current first-half odds, and lineup effects.
2. Estimate second-half scoring rates separately. Do not assume both halves have identical rates.
3. Enumerate plausible score pairs for each half and derive:
   - First-half home/draw/away probabilities.
   - First-half Asian handicap and total settlement probabilities.
   - Full-time home/draw/away probabilities.
   - The nine HT/FT joint probabilities: HH, HD, HA, DH, DD, DA, AH, AD, AA.
4. Apply a small state adjustment because second-half scoring depends on the half-time score. Leading teams usually reduce tempo; trailing teams increase attacking risk. State the adjustment when it materially changes a pick.
5. Remove bookmaker margin separately for the first-half and HT/FT markets. Cross-check the model against market probabilities rather than treating either as ground truth.

Use `H`, `D`, and `A` for home, draw, and away. Examples: `DD` = half-time draw/full-time draw; `DA` = half-time draw/full-time away win.

## EV and selection rules

- Decimal odds: `EV = probability * decimal_odds - 1`.
- Hong Kong odds: `EV = probability * hk_odds - (1 - probability)`.
- Settle quarter-goal first-half lines using their real half-win/half-loss components.
- For mutually exclusive HT/FT selections sold as a two-selection ticket, calculate each leg separately. The combined hit probability is the sum of the selected outcome probabilities, but expected return depends on the stake allocated to each leg. Do not add the two EV values.
- Recommend at most one first-half direction. For HT/FT, always output exactly two stability-selected scenarios whenever a nine-outcome model matrix is available.
- Treat first-half advice as actionable only when current odds are available, EV and no-vig edge are positive, data quality is medium/high, and the edge survives lineup-time reanalysis.
- Treat HT/FT as high variance. A formal path needs positive EV, positive no-vig edge, medium/high data quality, a complete current nine-outcome market, and data from at least five bookmakers. Apply the global 8% EV/4pp edge strict gate only to `against` or materially `conflicting` paths, and apply the HT/FT risk penalty in `stability-v1`.
- Before selection, require the matrix row sums to match the displayed first-half H/D/A probabilities and its column sums to match the displayed full-time H/D/A probabilities within 0.5 percentage points. Recalculate the matrix when either marginal check fails; do not publish or select from an inconsistent matrix.
- Use `scripts/htft_ranker.py` and `scenario_stability_v2`, not raw joint-probability order or EV order. Pass the result classes from the unconditional exact-score Top 2 for audit. First restrict eligible terminal results to the aggregate full-time H/D/A Top 2, including every result tied at the second-place cutoff. Then score paths using conditional follow-through `P(full-time result | half-time state)` at 45%, joint support at 30%, full-time marginal support at 15%, and a 10% same-state continuity bonus. The exact-score result classes may confirm or flag the selected pair but must not reserve a slot, and the continuity bonus cannot promote a third-ranked full-time result over coherent paths.
- Require half-time state support of at least 15%, joint support of at least 5%, and conditional follow-through of at least 25% for a fully supported scenario. If fewer than two paths pass, preserve two output slots but mark the fallback slot `稳定证据不足`; never fill it because of a long price or high EV.
- Positive EV and edge are qualification gates only after the two stable scenarios are selected. A selected scenario becomes formal only when every safety check passes. Treat a high-EV outcome outside the stable pair as a market anomaly to recheck, not as an automatic recommendation.
- Show the failed threshold for every observation candidate, for example `EV -2.5%` or `市场边际仅 +1.2pp`. An observation candidate is a stable match shape, not an actionable positive-EV bet.
- If current HT/FT odds are missing, keep the same two stability-selected scenarios as `赔率缺失，不可执行`; do not switch back to probability ranking and do not invent odds, market probability, or EV. If the model matrix itself cannot be calculated, mark both slots `数据不足`.
- Do not replace the two stable outputs with a generic `无正EV建议` or `观望`. Preserve the risk warning instead.

## Visual output

Add these sections after the full-time market analysis:

1. `半场判断`: first-half 1X2 probabilities, likely half-time scores, current half-time Asian/total lines, and the best positive-EV direction.
2. `半全场矩阵`: a compact 3x3 matrix for HH through AA, with row/column marginal checks and the two stability-selected scenarios highlighted.
3. `稳定形态`: show exactly two rows labelled `主稳定形态` and `备选稳定形态`, with selection, status, joint probability, conditional follow-through, state continuity, no-vig market probability, edge, odds, and EV. Do not show a rank column. Show combined hit probability only when outcomes are mutually exclusive and show the assumed stake split.
4. `风险`: missing odds, small samples, lineup uncertainty, and high variance.

Concise mode includes the best first-half direction plus both stability-selected HT/FT scenarios. Keep `观察候选（未达标）` labels even in concise mode.

## Calibration note from the supplied betting log

The supplied workbook contains six settled HT/FT tickets, not 206 matches. All six used half-time draw as the common branch and paired it with full-time draw/home/away. Two tickets won and four lost: a 33.3% ticket hit rate. Stakes totalled 2,000 and recorded ticket profit totalled 725 before the 9-unit rebate, a 36.25% stake-weighted ROI. The workbook's 73.4% figure is return on the initial 1,000-unit bankroll after top-up/rebate adjustments, not betting ROI.

The log does not contain per-leg odds, stake allocation, half-time score, or which branch settled as the winner. Use it to support the requested display and paired-outcome workflow only. Do not use six tickets to raise the prior probability of half-time draw or to train model weights.
