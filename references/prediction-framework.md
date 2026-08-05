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
- The three system-selected full-time branches beneath the leading half-time result, each paired with its own genuine representative score path from one validated posterior

### Mode B: 可视化模式 (Visual/Detailed)
Use one simple deterministic image plus analytical text. Initial and lineup-check images share
the same fixed eight columns: `编号`, `时间`, `赛事`, `主队 vs 客队`, `主推`, `总进球`, `半全场`,
and `波胆`. The renderer derives the date, title, and stage subtitle from the bound archive;
callers cannot supply them, mix stages/dates, or repeat one match. The stage does not change the table. In the image,
show only the highest-probability goal range with its probability and lead over second place.
Show HT/FT and exact score as a three-way branch tree from the same path posterior: use the
leading half-time result as the root, cover every full-time result once, and pair each branch
with its highest-probability genuine score path.

Keep the full analysis in accompanying text or machine audit, in this stable order:

1. Match identity, kickoff time/timezone, status, and lineup state
2. Opening-to-current Asian handicap, totals, 1X2, and available corner movement
3. Complete half-time, full-time 1X2, goal-range, and BTTS distributions with probability gaps
4. Unified EV comparison for supported candidate markets
5. Fundamentals, home/away split, H2H, motivation, lineup/injuries, and data quality
6. Primary/no-primary decision, secondary references, failed gates, and risks
7. The same system-selected paired joint events shown in the image
8. Internal HT/FT matrix and standalone score diagnostics only when audit detail is useful

Render probability bars with a fixed-width 20-block scale, for example:

```text
主胜  46%  █████████░░░░░░░░░░░
平局  29%  ██████░░░░░░░░░░░░░░
客胜  25%  █████░░░░░░░░░░░░░░░
```

Keep the visual hierarchy even when data is missing. Mark unavailable values as `数据未取得` or `待公布` instead of estimating them. If no validated joint-posterior artifact exists, show `数据不足` rather than rebuilding scenarios from separate fields or prose. Images must never use an ellipsis or three-dot truncation; wrap at semantic boundaries, reduce to a readable font size, grow the row, or expand the canvas. A completed review also receives a deterministic image in the same visual family, bound to the final active settlement basis and verified result; it must retain `主推：无正式推荐（不结算、不计战绩）` when applicable and must not rewrite the pre-match direction. Generate a separate HTML artifact only when the user asks for an HTML/report file or when interactive comparison is materially useful.

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

### Conditional market evidence

Bookmaker data may condition a match-path posterior only through a versioned method whose
weights, regularization, missing-market behavior, and calibration have passed strict
walk-forward or live-forward validation. Correlated 1X2, Asian, totals, first-half, and HT/FT
markets are not independent observations and must not be multiplied together with unit
weight. If a price participates in conditioning, it cannot be reused as independent EV/edge
evidence against the resulting posterior. Use a documented leave-one-bookmaker-out target
price, or label the comparison non-independent and ineligible. Initial-archive market evidence
may be at most 60 minutes old; lineup-check evidence may be at most 30 minutes old. Evidence
outside that stage-specific TTL fails closed instead of appearing as current. Incomplete,
post-kickoff, or uncalibrated market data remain audit context and receive zero conditioning
weight. A `model_only` posterior is independent of missing market evidence and must say so.

---

## Step 4: Model Prediction

Do not invent a Logistic Regression result from prose weights. The executable baseline is
`scripts/score_model.py`: a time-decayed attack/defence Poisson model with the Dixon-Coles
low-score correction. It produces the canonical regulation-time score prior. User-facing
football markets and scenarios require a separately versioned unified path posterior that
binds this prior to the half-time and second-half components.

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

### 4.3 Build the unified match-path posterior

Represent a match path as `(half_home, half_away, second_home, second_away)` so the full-time
score is determined by addition. Before IPF, run the complete Hall support-feasibility audit
for every full-time block and fail before fitting when a requested marginal cannot be carried
by the available path support. Build a non-negative normalized path distribution from the
registered half-time and second-half score components, then use a documented IPF or
minimum-KL procedure to reproduce the canonical full-time exact-score marginal, the verified
half-time marginal, and the HT/FT 3x3 marginal. New artifacts store a compact, hash-bound path
kernel instead of a duplicated list of every joint cell. Validation must reconstruct the full
four-dimensional paths and independently reproduce HT, second-half, FT, HT/FT, derived-market
and Top-2 outputs. The artifact must store its state definition, input hashes, construction
version, Hall audit, convergence and tail audit, and every recomputed marginal.

Rank user-facing scenarios only after aggregating this distribution to genuine
`(HT/FT × full-time score)` joint events. The joint probability is a sum of path cells, not the
product of an HT/FT marginal and a score marginal. Preserve the global joint Top 2 as an
internal audit. For public output, use the highest-probability half-time result as a common
root, cover all three full-time outcomes, select the highest-probability score path within
each HT/FT branch. Show the continuity branch first and the remaining full-time outcomes in
canonical H/D/A order; report conditional probabilities explicitly instead of implying that
the structural order is a probability ranking. Keep HT/FT and score in the same order so
every displayed position remains one event.
Derive half-time, 1X2, total goals, goal range, and BTTS from the same artifact. Preserve legacy
unconditional exact-score and HT/FT Top 2 fields only as machine-readable diagnostics; never
place them side by side as user scenarios, reorder them for terminal-result agreement, or fill
a missing joint event by hand.

If the artifact fails fixture, hash, cutoff, timing, normalization, tail, convergence, Hall
feasibility, kernel reconstruction, or marginal consistency validation, publish `数据不足`
and no joint scenario. A model average,
old archive, prose conclusion, or terminal-result filter is not an accepted fallback.

### 4.4 Market calibration is separate from the football model

Read `<workspace>/.codex/soccer-predict/calibration.json` for forward-test metrics and
guardrails. It does not modify the fitted score model. By default, use market prices only to
calculate a no-vig comparison probability and EV. A versioned strictly forward-calibrated
conditioner may update the unified path posterior, but it must preserve the provenance of every
market input and may not claim the same conditioning price as independent edge evidence.

### Market-alignment gate

The current strict forward policy keeps Asian handicap, first-half, HT/FT, corner-total, and
corner-handicap directions
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

Read [expanded-markets.md](expanded-markets.md). Goal ranges and BTTS need a complete mutually exclusive current market plus chance-quality or confirmed attacking-configuration evidence. Corner totals and handicaps need a complete two-way market from at least three firms plus independent corner-profile evidence even for diagnostic qualification. The current corner manager is historical-only and returns `formal_corner_total_eligible=false` and `formal_corner_handicap_eligible=false`, so these candidates remain observations until a future manager binds separate strict live-forward validation. A historical percentage, raw goals average, possession figure, or isolated price cannot satisfy these gates alone.

Build one pool only from candidates that pass every audit and market-specific gate. Use
the versioned selection policy emitted by `memory_store.py`; do not rank with an informal
weighted paragraph. Model probability may influence EV and settlement risk, but it is not
three independent pieces of evidence. Count EV once, use edge as a qualification/audit
check, and use genuinely separate tie-breakers such as settlement variance, data freshness,
market depth, source agreement, and out-of-sample calibration.

Select exactly one primary only when the top candidate remains best under reasonable price
and probability sensitivity. Otherwise use no primary. Never force a direction merely to
increase the number of predictions.

### 4.5 Lineup and contextual effects

Do not add fixed goal increments such as “goalkeeper absent = +0.75 goals” unless that
coefficient was learned from the training population and versioned in the model. Confirmed
lineup, injuries, weather, motivation, and rest may be reported as evidence and used for a
documented sensitivity scenario. When the executable model cannot consume that feature,
keep the unadjusted baseline, show the scenario range separately, and downgrade a formal
pick whose sign changes across the range.

---

## Step 5: Win Probability & Betting Advice

Read [expanded-markets.md](expanded-markets.md), [exact-score.md](exact-score.md), and [half-time-full-time.md](half-time-full-time.md). Calculate every football-goal market and build the user-facing three-way branch tree from one validated match-path posterior. Use the leading half-time result as the root, cover all three full-time outcomes, and pair each branch with its own highest-probability score path. Keep standalone exact-score, global joint Top 2, and HT/FT rankings as internal diagnostics only. Calculate the independent corner markets from their own required distribution.

### Win Probability Prediction
Aggregate the validated unified path posterior; use a market-conditioned posterior only under the calibrated and provenance-safe rules above:
- Asian handicap: full-win, half-win, push, half-loss, and full-loss probabilities for the exact side and line
- Over/Under: the same five settlement states for the exact side and line
- Goal ranges: sum every matching cell in the complete football score matrix
- BTTS: sum cells where both teams score; the opposite side is its complement
- 1X2 and exact score: sum or rank the full-time marginal of the same path posterior
- HT/FT and joint display events: sum the same path cells, never combine independently ranked marginals
- Corner total and handicap: derive from independent total-corner and corner-margin distributions and never bind them to a goal path without a separately validated cross-model joint distribution

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
1. Headline direction in archive-derived order: best threshold-qualified formal primary; otherwise the highest-ranked separately qualified observation as `◇ 观察/不下注`; otherwise the validated joint 1X2 Top-1 as `◇ 模型首选（不计主推、不计战绩）`; otherwise `数据不足`. Never show both non-primary fallbacks as competing headline directions.
2. One highest-probability goal range plus its probability lead in the compact image; retain the complete goal-range distribution in text/audit
3. Exactly three user-facing branches under the leading half-time result, each paired with its own highest-probability genuine full-time score path; if the artifact is unavailable, show `数据不足` and no fallback scenarios
4. Confidence level for each recommendation and the best qualified first-half direction, or `无正EV建议`
5. Complete half-time, 1X2 and BTTS distributions in text/audit, plus a 3x3 HT/FT marginal matrix when audit detail is useful. The legacy `probability_top2_v3_post_selection` HT/FT Top 2 may remain archived for component evaluation but is not a user-facing scenario list. Pass `league_key`, current model hash, and registry-issued evidence; historical evidence remains descriptive, and every league stays `production_confidence_eligible=false` without clean live-forward confirmation and complete executable nine-way price history. A half-time-market-anchored matrix has no promoted pair-mass gate; label it `anchor_gate_unvalidated`. Under `strict-oos-market-policy-v1`, HT/FT remains observation-only regardless of diagnostic gates.

Treat joint events and standalone exact-score diagnostics only as high-variance references. Never include their Top-1 or Top-2 hits in primary-pick or all-formal accuracy/ROI.

---

## Step 6: Codex 存档（MANDATORY）

**每次预测完成后必须执行，不可跳过。**

仅对赛前预测运行 `scripts/memory_store.py record`，保存到当前工作区：

`<workspace>/.codex/soccer-predict/history.json`

记录至少包含：

- 比赛 ID、联赛、带时区的开球时间
- 主客队、绑定的后验 artifact/hash，以及领先半场状态下“延续结果优先、其余按 H/D/A 枚举”的三条全场分支；每条分支另列条件概率，并配对其联合概率最高的真实全场比分路径
- 亚盘选择方、盘口、赔率
- 大小球方向、盘口、赔率
- 合格的总进球区间、双方进球、角球大小或角球让球方向及其真实市场赔率
- 胜平负概率、推荐概率和 EV
- 推荐、来源 URL、关键理由
- 数据质量，以及每个正式推荐相对临场市场的 `aligned/neutral/against/conflicting/unknown` 分类

通过两个 `--exact-score-pick SCORE:PROBABILITY` 保存无条件比分 Top 2，仅作为机器可读的全场分布审计，并让 `--predicted-score` 等于无条件第一项；这些字段不得直接驱动卡片。面向用户的配对情景必须另行绑定通过校验的联合路径后验 artifact；展示层选择概率最高的半场状态，完整覆盖其后的全场胜、平、负三路，并为每一路配对该分支联合概率最高的真实比分路径。没有该 artifact 时显示 `数据不足`。只有通过阈值且被当前市场政策与对应 manager formal flag 放行的正式推荐才能写入专用 pick 字段；当前角球 manager 的两个 formal flag 均为 false，因此角球只能进入观察审计，不能写入 `corner_total_pick`、`corner_handicap_pick` 或成为主推，也不能与联合进球路径硬绑定。每次调用必须通过 `--primary-market` 明确全市场唯一主推；脚本把其余合格方向标为 `secondary`。若没有正式方向，显式传 `--primary-market none`。波胆、联合情景和观察候选不得计入正式准确率或 ROI。滚球或赛后分析不得伪装为赛前预测，也不得计入准确率。

**不存档 = 工作流未完成。**
