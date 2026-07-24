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
- Recommend at most one first-half direction. For HT/FT, always output exactly two ranked suggestions whenever a nine-outcome model matrix is available.
- Treat first-half advice as actionable only when current odds are available and the probability edge survives lineup-time reanalysis. During the active small-sample protection period, an archived first-half primary requires EV >= 8%, model-versus-market edge >= 4pp, and medium/high data quality; lower positive EV is observation only.
- Treat HT/FT as high variance. Require model EV of at least 8%, a model-versus-market edge of at least 4 percentage points, and data from at least five bookmakers for a `正式推荐`.
- Rank all nine outcomes by EV descending. Put threshold-qualified outcomes first; break ties by model-versus-market edge, then model probability. Fill any remaining slot among the top two with the best unqualified outcome and label it `观察候选（未达标）`.
- Show the failed threshold for every observation candidate, for example `EV -2.5%` or `市场边际仅 +1.2pp`. An observation candidate is a ranked model direction, not an actionable positive-EV bet.
- If current HT/FT odds are missing, show the two highest model-probability outcomes as `赔率缺失，不可执行`; do not invent odds, market probability, or EV. If the model matrix itself cannot be calculated, mark both slots `数据不足`.
- Do not replace the two ranked outputs with a generic `无正EV建议` or `观望`. Preserve the risk warning instead.

## Visual output

Add these sections after the full-time market analysis:

1. `半场判断`: first-half 1X2 probabilities, likely half-time scores, current half-time Asian/total lines, and the best positive-EV direction.
2. `半全场矩阵`: a compact 3x3 matrix for HH through AA, with the two ranked suggestions highlighted.
3. `组合建议`: show exactly two rows with rank, selection, status, probability, no-vig market probability, edge, odds, and EV. Show combined hit probability only when outcomes are mutually exclusive and show the assumed stake split.
4. `风险`: missing odds, small samples, lineup uncertainty, and high variance.

Concise mode includes the best first-half direction plus both ranked HT/FT suggestions. Keep `观察候选（未达标）` labels even in concise mode.

## Calibration note from the supplied betting log

The supplied workbook contains six settled HT/FT tickets, not 206 matches. All six used half-time draw as the common branch and paired it with full-time draw/home/away. Two tickets won and four lost: a 33.3% ticket hit rate. Stakes totalled 2,000 and recorded ticket profit totalled 725 before the 9-unit rebate, a 36.25% stake-weighted ROI. The workbook's 73.4% figure is return on the initial 1,000-unit bankroll after top-up/rebate adjustments, not betting ROI.

The log does not contain per-leg odds, stake allocation, half-time score, or which branch settled as the winner. Use it to support the requested display and paired-outcome workflow only. Do not use six tickets to raise the prior probability of half-time draw or to train model weights.
