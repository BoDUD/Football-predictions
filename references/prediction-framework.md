# Prediction Framework

## Table of Contents
1. [Output Modes](#output-modes)
2. [Step 1: Data Organization](#step-1-data-organization)
3. [Step 2: Fundamental Analysis](#step-2-fundamental-analysis)
4. [Step 3: Odds Probability Calculation](#step-3-odds-probability-calculation)
5. [Step 4: Model Prediction](#step-4-model-prediction)
6. [Step 5: Win Probability & Betting Advice](#step-5-win-probability--betting-advice)

---

## Output Modes

Use the following output policy:

- **Default is always Mode B: 可视化模式.** Keep using it across later predictions and post-match reviews without asking again.
- Switch to Mode A only when the user explicitly requests `简洁模式`, `简洁`, `concise`, or `short`.
- A previous one-off request for concise output does not change the skill's default for future requests.

### Mode A: 简洁模式 (Concise)
Quick prediction results only - best for fast decisions:
- Match info summary
- Key odds data (main lines)
- Best pick with probability and EV
- Exactly two user-facing exact-score scenarios. For a formal total primary, rank them inside the primary's net-profit branch and show both full-match probability and conditional share; otherwise use the unconditional Top 2

### Mode B: 可视化模式 (Visual/Detailed)
Full analysis with compact Markdown tables and probability bars. Use this stable order:

1. Match card: competition, kickoff time/timezone, status, home and away teams
2. Market movement table: opening vs current Asian handicap, totals, and available corner lines, with direction labels
3. 1X2 market table: representative/consensus odds, removed margin, and normalized probabilities
4. Probability bars: home/draw/away plus the strongest available football and corner candidates
5. Unified EV comparison table: Asian handicap, totals, goal ranges, BTTS, corner totals, corner handicaps, first-half, and qualified HT/FT candidates
6. Evidence panel: recent form, home/away split, H2H, motivation, lineup/injuries, and data quality
7. Decision card: primary pick, secondary lean, key reasons, and risks
8. Exact-score panel: exactly two user-facing scenarios with unconditional model probability and `高方差参考（不计主推）`. For a formal total primary, also show `主推成立时` conditional share; do not add a separate 0-0 row unless explicitly requested
9. Half-time panel: first-half probabilities, likely half-time scores, first-half Asian/total lines, and the best qualified direction
10. HT/FT matrix: HH through AA probabilities and current odds/EV, followed by exactly two stability-selected scenarios labelled `主稳定形态` and `备选稳定形态`. Mark each as `正式推荐` or `观察候选（未达标）`; do not show a rank column.

Render probability bars with a fixed-width 20-block scale, for example:

```text
主胜  46%  █████████░░░░░░░░░░░
平局  29%  ██████░░░░░░░░░░░░░░
客胜  25%  █████░░░░░░░░░░░░░░░
```

Keep the visual hierarchy even when data is missing. Mark unavailable values as `数据未取得` or `待公布` instead of estimating them. Generate a separate HTML artifact only when the user asks for an HTML/report file or when interactive comparison is materially useful.

---

## Step 1: Data Organization

Organize all collected data into these categories:

### 1. Fundamentals
- Recent form (last 5-10 matches: W/D/L, goals scored/conceded)
- Home/away performance (home win% / away win%)
- Head-to-head records (last 3-5 years: W/D/L, goal trends)
- League standings and points gap
- Both teams' match motivation
- **If lineup not yet published**: Note "阵容未公布" and proceed with available data

### 2. Squad & Lineup
- Starting XI (if available - typically 30-60 min before kickoff)
- Key player stats (goals/assists)
- Injury/suspension list (especially core player absence impact)
- Bench depth (substitute player quality)
- **If unavailable**: Mark as "待公布" and use squad depth info only

### 3. Match Importance
- Both teams' motivation (relegation battle / title race / playoff fight)

### 4. European Odds (1X2)
- Complete home/draw/away odds data ("即" = instant and "早" = early/opening rows)
- **IMPORTANT**: Use "即" (instant/live) data for final calculation

### 5. Asian Handicap
- Complete handicap data ("即" and "早" rows)
- **CRITICAL**: Must record BOTH "早" (early) AND "即" (instant) data
- **FINAL CALCULATION**: Use "即" (instant/live) data only - this is the final odds before match
- Analyze line movement: if "早" → "即" changed (up/down), record the trend
- Rule: the team with lower European odds corresponds to the handicap-giving side (upper plate)
- Upper plate odds are on the handicap-giving team's side

### 6. Over/Under
- Complete over/under data ("即" and "早" rows)
- **FINAL CALCULATION**: Use "即" (instant/live) data for final calculation

### 7. Enhanced Data (for Over/Under Model)
- Half-time goals patterns (半场进球模式)
- Corner kicks statistics (角球数据)
- Goal difference distribution (净胜球分布)

### 8. Expanded Markets

- Complete current goal-range and BTTS outcome prices, or an explicit unavailable/incomplete marker
- Complete two-way corner-total and corner-handicap prices from at least three firms
- Source URL, timestamp, bookmaker count, odds format, and market-completeness flag
- Independent corner-profile inputs: home/away corners for and against, width/crossing, dangerous attacks, set pieces, match-state tendency, and confirmed personnel

---

## Step 2: Fundamental Analysis

Perform deep analysis based on collected data:

### 2.1 Handicap Rationality Check
Use the fitted score-distribution baseline to calculate a goal-margin distribution and
the settlement probabilities of the current line. A fair handicap is a model diagnostic,
not a manually chosen label. If there is no fitted model artifact or the home/away team is
outside its supported population, mark the model unavailable and do not publish a formal
handicap direction.

### 2.2 Line Movement Tracking (盘口走势分析)
- Record early odds ("早" = opening line) and instant odds ("即" = current line)
- Analyze changes: "早" → "即" direction (upgraded/downgraded/no change)
- **Example**: If early line was 平手(0) and instant line is 半球/一球(-0.75), record as "升盘"
- Determine the true purpose behind odds movements:
  - Upgraded line (升盘): typically indicates stronger team being favored
  - Downgraded line (降盘): typically indicates weaker team being favored
- Water adjustment: odds movement without line change

### 2.3 Evidence hygiene

- If an injury list conflicts with the confirmed starting XI, trust the confirmed XI and discard the stale injury item as support for handicap or totals conclusions.
- Never use a totals price drop alone as primary-pick evidence. Require consistency across multiple firms plus corroborating attacking configuration or chance-quality evidence.

### 2.4 Market-movement interpretation
- Report only observable changes in line, price, bookmaker coverage, and timestamp.
- Do not claim to know bookmaker intent, sharp money, public money, or a trap unless a
  named source actually provides that information.

### 2.5 Betting Volume Analysis
- Use betting-volume data only when a named source, collection time, and definition are
  available. Price movement is not a substitute for volume data.
- Mark unsupported sharp/public-money narratives as unavailable rather than using them as
  evidence or model features.

### 2.6 European-to-Asian Odds Conversion
- Convert European odds to Asian handicap and odds
- Check if Asian handicap matches the converted values
- Treat a mismatch as a reproducible cross-market diagnostic. Do not label it a trap or
  convert it directly into a recommendation.

---

## Step 3: Odds Probability Calculation

**CRITICAL**: Use INSTANT odds ("即") for final calculation - this represents the final odds before match kickoff.

### Odds-format-safe no-vig probabilities

Never apply one formula to an unspecified price. Convert each price to decimal first:

```text
decimal_price = quoted_price                 # decimal odds
decimal_price = 1 + quoted_price             # Hong Kong odds
raw_implied_i = 1 / decimal_price_i
no_vig_market_probability_i = raw_implied_i / sum(raw_implied_all_outcomes)
overround = sum(raw_implied_all_outcomes) - 1
```

Use only a complete mutually exclusive market collected from the same source and time
window. For two-way Asian handicap and totals rows, preserve both sides, the exact line,
odds format, bookmaker count, price basis, source URL, and timezone-aware collection time.
For 1X2 and HT/FT, all three or all nine outcomes are required respectively.

The no-vig number is a market comparison baseline, not the model's probability and not a
literal win probability on lines with push or split settlement. The fitted score matrix
supplies `full_win`, `half_win`, `push`, `half_loss`, and `full_loss`; the current market
supplies price and no-vig comparison probability. Keep those concepts separate.

---

## Step 4: Model Prediction

Do not invent a Logistic Regression result from prose weights. The executable baseline is
`scripts/score_model.py`: a time-decayed attack/defence Poisson model with the Dixon-Coles
low-score correction. It produces one canonical regulation-time score matrix from which
every football-goal market is derived.

### 4.1 Fit a versioned baseline

Use completed matches whose event time is strictly earlier than the fixture being predicted.
The minimum CSV columns are `date,home_team,away_team,home_goals,away_goals`.

```text
python scripts/score_model.py fit --input history.csv --output model.json \
  --half-life-days 365 --iterations 1200 --learning-rate 0.03 --regularization 0.02
```

The artifact records its schema/model version, training window, configuration, fitted team
parameters, data hash, and model hash. Never edit fitted parameters by hand or call a
`calibrate` summary a trained model. Retrain only with a documented new data cutoff.

### 4.2 Produce the canonical score matrix

```text
python scripts/score_model.py predict --model model.json \
  --home-team HOME --away-team AWAY --kickoff 2030-08-10T19:00:00+09:00 \
  --output prediction.json \
  --total over:2.25 --asian home:-0.75
```

Prediction refuses a model whose training cutoff is on or after the fixture's UTC date and
refuses an artifact generated at or after kickoff. Output includes expected goals, the normalized finite score matrix, pre-
normalization tail mass, 1X2, BTTS, goal ranges, exact scores, and any requested total or
Asian settlement distribution. Unknown teams fail closed unless
`--unknown-team-policy league_average` is explicitly selected; that fallback must remain in
the warnings and lowers data quality.

Before using an artifact, verify all cells are finite and non-negative, matrix mass is one
within tolerance, reported tail mass is below the configured threshold, and every derived
market reproduces the relevant cell sum. Archive the prediction artifact/model provenance
with the formal record. A hand-computed probability may be shown as an observation but may
not become a formal pick.

### 4.3 Market calibration is separate from the football model

Read `<workspace>/.codex/soccer-predict/calibration.json` for forward-test metrics and
guardrails. It does not modify the fitted score model. Use market prices only to calculate a
no-vig comparison probability and EV; never blend the price back into the score matrix and
then claim the resulting edge is independent.

### Market-alignment gate

The current strict forward policy keeps Asian handicap, first-half, and HT/FT directions
`observation_only`. They may be calculated and archived for calibration, but cannot become a
formal primary until a later versioned policy is supported by enough clean out-of-sample
records. Legacy or backfilled wins do not count toward re-enabling a market.

Classify each candidate against the consensus opening-to-current move:

- `aligned`: line and related no-vig market movement support the selection.
- `neutral`: no material move.
- `against`: both line and related market probabilities move materially against the selection.
- `conflicting`: Asian/total and European signals disagree.
- `unknown`: insufficient comparable bookmaker data.

An ordinary formal direction needs positive current EV, positive model-versus-market edge, medium/high data quality, a complete executable current market, and all applicable market-specific evidence. Missing or non-positive inputs, incomplete prices, or an `unknown` signal make it `观察候选（未达标）/不下注`.

An `against` or materially `conflicting` direction is the strict exception: require EV >= 8%, edge >= 4pp, at least five bookmakers, and independent confirmed-lineup or fundamental corroboration. Keep these safeguards until a feature-level review explicitly changes them; reaching 20 graded selections does not relax them automatically or create a weight change.

### Deep-favorite cover gate

For a selected favorite at `-0.75` or deeper:

1. Calculate the goal-margin distribution directly and report the selected line's full-win, half-win, push, half-loss, and loss probabilities when applicable.
2. Require high data quality and confirmed lineups. Prefer independent chance-quality evidence such as non-penalty xG, big chances, shots on target, or comparable chance creation. If it is unavailable, accept an aligned market from at least five firms only when confirmed attacking configuration and fundamental evidence also support the favorite.
3. Stress-test the opponent's counterattack, goalkeeper, set-piece, and early-concession tail risks.
4. Do not use 1X2 win probability, possession, reputation, or a strong XI as a proxy for cover probability.

If any item is missing, downgrade the deep favorite to observation even when the team is likely to win.

### Totals evidence gate

Any formal total-goals direction needs consensus from at least five firms and either independent chance-quality evidence or a confirmed attacking configuration. Historical over rates, H2H scores, a single price drop, or a stale injury list are context only and cannot satisfy the gate. If a confirmed XI contradicts the injury list, discard the stale injury effect and recalculate before recommending.

### Expanded-market evidence gate

Read [expanded-markets.md](expanded-markets.md). Goal ranges and BTTS need a complete mutually exclusive current market plus chance-quality or confirmed attacking-configuration evidence. Corner totals and handicaps need a complete two-way market from at least three firms plus independent corner-profile evidence. A historical percentage, raw goals average, possession figure, or isolated price cannot satisfy these gates alone.

Build one pool only from candidates that pass every audit and market-specific gate. Use
the versioned selection policy emitted by `memory_store.py`; do not rank with an informal
weighted paragraph. Model probability may influence EV and settlement risk, but it is not
three independent pieces of evidence. Count EV once, use edge as a qualification/audit
check, and use genuinely separate tie-breakers such as settlement variance, data freshness,
market depth, source agreement, and out-of-sample calibration.

Select exactly one primary only when the top candidate remains best under reasonable price
and probability sensitivity. Otherwise use no primary. Never force a direction merely to
increase the number of predictions.

### 4.4 Lineup and contextual effects

Do not add fixed goal increments such as “goalkeeper absent = +0.75 goals” unless that
coefficient was learned from the training population and versioned in the model. Confirmed
lineup, injuries, weather, motivation, and rest may be reported as evidence and used for a
documented sensitivity scenario. When the executable model cannot consume that feature,
keep the unadjusted baseline, show the scenario range separately, and downgrade a formal
pick whose sign changes across the range.

---

## Step 5: Win Probability & Betting Advice

Read [expanded-markets.md](expanded-markets.md), [exact-score.md](exact-score.md), and [half-time-full-time.md](half-time-full-time.md). Calculate the expanded markets from their required distributions, calculate two exact-score candidates for every valid pre-match model, then calculate first-half and HT/FT markets when the required data is available.

### Win Probability Prediction
Aggregate the canonical score matrix without blending bookmaker prices into it:
- Asian handicap: full-win, half-win, push, half-loss, and full-loss probabilities for the exact side and line
- Over/Under: the same five settlement states for the exact side and line
- Goal ranges: sum every matching cell in the complete football score matrix
- BTTS: sum cells where both teams score; the opposite side is its complement
- 1X2 and exact score: sum or rank cells from this same matrix
- Corner total and handicap: derive from independent total-corner and corner-margin distributions

### Expected Value (EV) Calculation

```
net_odds(decimal O) = O - 1
net_odds(Hong Kong O) = O
No-push EV = P(win) * net_odds - P(loss)
Quarter-line EV = P(full_win) * net_odds
                + P(half_win) * net_odds / 2
                - P(half_loss) / 2
                - P(full_loss)
edge_pp = 100 * (model_probability - no_vig_market_probability)
```

Push contributes zero profit. Require all five states, including explicit zeros, to sum to
one and reject states impossible for the line type. Recalculate EV and edge inside the
archive command from the probability distribution, complete current market, and odds
format. A caller-supplied value is only an assertion to audit; it is never authoritative.

### Final Output
1. Best threshold-qualified recommendation from the unified market pool; if none qualifies, show the highest-ranked observation as `不下注`
2. Exactly two user-facing exact-score scenarios. Preserve the unconditional Top 2 internally; for a formal total primary, display the two highest-probability net-profit scenarios and label the conditioning explicitly
3. Confidence level for each recommendation
4. Best qualified first-half direction, or `无正EV建议`
5. A 3x3 HT/FT probability matrix and exactly two stability-selected HT/FT scenarios whenever the matrix can be calculated. Validate its half-time row and full-time column marginals, select with `scenario_stability_v2`, and gate terminal results to the aggregate full-time Top 2 before stability ranking, including ties at the cutoff. Use exact-score result classes only as a consistency audit and EV only to classify either selected scenario as formal or observation. Never let raw joint probability, exact-score slot coverage, or a long price choose the pair.

Treat both exact scores only as shape/scenario references. Never include Top-1 or Top-2 exact-score hits in primary-pick or all-formal accuracy/ROI.

---

## Step 6: Codex 存档（MANDATORY）

**每次预测完成后必须执行，不可跳过。**

仅对赛前预测运行 `scripts/memory_store.py record`，保存到当前工作区：

`<workspace>/.codex/soccer-predict/history.json`

记录至少包含：

- 比赛 ID、联赛、带时区的开球时间
- 主客队和两个按概率排序的预测比分
- 亚盘选择方、盘口、赔率
- 大小球方向、盘口、赔率
- 合格的总进球区间、双方进球、角球大小或角球让球方向及其真实市场赔率
- 胜平负概率、推荐概率和 EV
- 推荐、来源 URL、关键理由
- 数据质量，以及每个正式推荐相对临场市场的 `aligned/neutral/against/conflicting/unknown` 分类

通过两个 `--exact-score-pick SCORE:PROBABILITY` 保存无条件 Top 2，并让 `--predicted-score` 等于无条件第一项。若唯一主推是全场大小球，再通过两个 `--display-exact-score-pick SCORE:PROBABILITY:UNCONDITIONAL_RANK` 和 `--display-exact-score-event-probability` 保存面向用户的主推条件场景。只有通过阈值的正式推荐写入 `asian_pick`、`total_pick`、`goal_range_pick`、`btts_pick`、`corner_total_pick`、`corner_handicap_pick`、`half_time_pick` 和 `htft_picks`。每次调用必须通过 `--primary-market` 明确全市场唯一主推；脚本把其余合格方向标为 `secondary`。若没有正式方向，显式传 `--primary-market none`。波胆和观察候选不得计入正式准确率或 ROI。滚球或赛后分析不得伪装为赛前预测，也不得计入准确率。

**不存档 = 工作流未完成。**
