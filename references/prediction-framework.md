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
- Exactly two ranked exact-score candidates with model probability

### Mode B: 可视化模式 (Visual/Detailed)
Full analysis with compact Markdown tables and probability bars. Use this stable order:

1. Match card: competition, kickoff time/timezone, status, home and away teams
2. Market movement table: opening vs current Asian handicap and over/under, with direction labels
3. 1X2 market table: representative/consensus odds, removed margin, and normalized probabilities
4. Probability bars: home/draw/away, handicap sides, and over/under sides
5. EV comparison table: direction, line, odds, model probability, EV, and confidence
6. Evidence panel: recent form, home/away split, H2H, motivation, lineup/injuries, and data quality
7. Decision card: primary pick, secondary lean, key reasons, and risks
8. Exact-score panel: exactly two ranked candidates with model probability and `高方差参考（不计主推）`
9. Half-time panel: first-half probabilities, likely half-time scores, first-half Asian/total lines, and the best qualified direction
10. HT/FT matrix: HH through AA probabilities and current odds/EV, followed by exactly two ranked suggestions. Mark each as `正式推荐` or `观察候选（未达标）`.

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

---

## Step 2: Fundamental Analysis

Perform deep analysis based on collected data:

### 2.1 Handicap Rationality Check
Use machine learning baseline model to analyze whether the handicap is reasonable.
- Determine if handicap is set deep (high) or shallow (low)
- Output the analyzed fair handicap value

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

### 2.4 Bookmaker Intent Analysis
- Analyze the real intention behind bookmaker adjustments
- Look for patterns in how lines have moved from opening ("早") to current ("即")

### 2.5 Betting Volume Analysis
- Use odds data to analyze betting volume changes
- Capture abnormal movements (sharp money, public money divergence)

### 2.6 European-to-Asian Odds Conversion
- Convert European odds to Asian handicap and odds
- Check if Asian handicap matches the converted values
- Identify potential trap lines (诱盘) where there's a mismatch

---

## Step 3: Odds Probability Calculation

**CRITICAL**: Use INSTANT odds ("即") for final calculation - this represents the final odds before match kickoff.

### Asian Handicap Probability
Calculate from latest (instant) Asian handicap data ("即" row):

```
1. Home win implied probability:    P(home) = 1 / (1 + home_odds)
2. Away win implied probability:    P(away) = 1 / (1 + away_odds)
3. Total implied probability:       P(total) = P(home) + P(away)
4. Home true implied probability:   P(true_home) = P(home) / P(total)
5. Away true implied probability:   P(true_away) = P(away) / P(total)
6. Margin (juice):                 P(margin) = 1 - 1 / P(total)
```

### Over/Under Probability
Same calculation method applied to instant over/under odds ("即" row):

```
1. Over implied probability:        P(over) = 1 / (1 + over_odds)
2. Under implied probability:       P(under) = 1 / (1 + under_odds)
3. Total implied probability:       P(total) = P(over) + P(under)
4. Over true implied probability:  P(true_over) = P(over) / P(total)
5. Under true implied probability: P(true_under) = P(under) / P(total)
6. Margin (juice):                 P(margin) = 1 - 1 / P(total)
```

---

## Step 4: Model Prediction

Before applying weights, read `<workspace>/.codex/soccer-predict/calibration.json` when present. Use its guardrails immediately. Use `active_weight_adjustments` only when that market is marked eligible and the adjustment is tied to feature-level review evidence.

### Initial Weight Allocation (Based on AI Probability Assessment)

**Weight principles**: Allocate from the baseline below. Post-match reviews may change durable weights only after at least 20 graded selections in that market and a feature-level error analysis. Until then, preserve these weights and update only provisional guardrails.

Default weights (initial):
| Feature | Asian Handicap | Over/Under |
|---------|:-------------:|:----------:|
| Odds implied probability | 0.35 | 0.15 |
| Fundamental analysis | 0.20 | 0.10 |
| Team fundamentals | 0.20 | 0.10 |
| Squad power decay | 0.20 | 0.05 | <!-- v1.1: up from 0.15 -->
| Motivation | 0.10 | 0.05 |
| Enhanced data (half-goals/corners) | - | 0.15 |
| Environment factor | - | 0.10 |
| League factor | - | 0.10 |
| **Defense injury coefficient** | - | **0.15** | <!-- v1.1: new -->
| Other | - | 0.05 |

**Note**: `memory_store.py calibrate --write` records whether the sample is eligible for a weight change. Never claim that prose alone trained or optimized the model.

### Market-alignment gate

Classify each candidate against the consensus opening-to-current move:

- `aligned`: line and related no-vig market movement support the selection.
- `neutral`: no material move.
- `against`: both line and related market probabilities move materially against the selection.
- `conflicting`: Asian/total and European signals disagree.
- `unknown`: insufficient comparable bookmaker data.

A normal full-time formal recommendation needs EV >= 5%, model-versus-market edge >= 3pp, and medium/high data quality. A candidate with an `against` signal needs EV >= 8%, edge >= 4pp, at least five bookmakers, and independent lineup or fundamental corroboration. The primary pick gets no exemption from this gate. Otherwise show it as `观察候选（未达标）/不下注` and do not archive it as a pick.

### 4.1 Asian Handicap Logistic Regression Model

Perform deep analysis using logistic regression. Quantify input features, standardize, and output comprehensive prediction probability.

**Analysis weight priority** (descending):
```
Odds data > Fundamental analysis > Real-time lineup > Motivation
(Bookmaker info > Short-term disruption > Long-term trends > Subjective factors)
```

**Input features**:
| Feature | Description |
|---------|-------------|
| Team fundamentals | Recent win rate, home/away differential |
| Squad power decay coefficient | Calculated from injury/suspension list |
| Motivation label | 0-1 standardized |
| Fundamental analysis | Results from Step 2 |
| Odds implied probability | **Core feature** from Step 3 |

### 4.2 Over/Under Logistic Regression Model (Enhanced)

Deep analysis of over/under handicap using logistic regression with enhanced features.

**Key comparison**: If opened line > analyzed line -> high open; If opened line < analyzed line -> shallow open.

**Input features** (for both home and away teams):
| Feature | Weight | Description |
|---------|:------:|-------------|
| xG and xGA | 0.12 | Expected goals and expected goals against |
| League factor | 0.10 | League-specific scoring patterns (MLS~55% over, etc.) |
| Recent win rate | 0.05 | Last N matches |
| Recent 5-match goals | 0.07 | Goals in last 5 games (reduced from 0.10 — low recent goals ≠ low match goals under injury conditions) |
| H2H history | 0.05 | Head-to-head goal patterns |
| Home/away differential | 0.10 | Home vs away scoring difference |
| Squad power decay coefficient | 0.05 | From injury/suspension list (attacking side only) |
| **Defense injury coefficient** | **0.15** | **🆕 KEY RULE: GK absence → +0.75~1 goal adj; CB absence → +0.5; DM absence → +0.25** |
| Motivation label | 0.05 | 0-1 standardized |
| Environment factor | 0.10 | Weather, venue altitude, rest days |
| Half-time goals pattern | 0.08 | Half-time scoring behavior (high-scoring half vs low) |
| Corner kicks | 0.08 | Corner kick data (indicates attacking intensity) |
| Fundamental analysis | 0.10 | Results from Step 2 |
| Odds implied probability | 0.15 | **Core feature** from Step 3 |

**🆕 Defense Injury Rule (v1.1)**:
When a team is missing key defensive players, the over/under model MUST adjust upward:
- **GK absent** (致命级): +0.75 to +1.0 goal adjustment toward OVER. This is the single most impactful injury type for goals.
- **CB absent** (严重级): +0.5 goal adjustment
- **DM absent** (中等级): +0.25 goal adjustment
- **FB absent** (轻微级): +0.1 goal adjustment
- Stacking: multiple positions missing → adjustments stack (e.g., GK + DM = +1.0 to +1.25)
- **Critical correction**: Defense injuries do NOT mean "both teams score less → under". The correct interpretation is "conceding team leaks more goals → toward OVER". Attacking injuries only affect that team's scoring, they do NOT cancel out opponent's defensive collapse.

**Output**: Predicted home goals, away goals, and total goals.

---

## Step 5: Win Probability & Betting Advice

Read [exact-score.md](exact-score.md) and [half-time-full-time.md](half-time-full-time.md). Calculate two exact-score candidates for every valid pre-match model, then calculate first-half and HT/FT markets when the required data is available.

### Win Probability Prediction
Combine odds analysis and model analysis to predict:
- Asian Handicap: P(home_win) and P(away_win)
- Over/Under: P(over_win) and P(under_win)

### Expected Value (EV) Calculation

```
Asian Handicap Home EV = P(home_win) * home_odds - P(away_win)
Asian Handicap Away EV = P(away_win) * away_odds - P(home_win)
Over EV               = P(over_win) * over_odds - P(under_win)
Under EV              = P(under_win) * under_odds - P(over_win)
```

### Final Output
1. Best threshold-qualified betting recommendation; if none qualifies, show the highest-ranked observation as `不下注`
2. Exactly two ranked exact-score candidates with model probability
3. Confidence level for each recommendation
4. Best qualified first-half direction, or `无正EV建议`
5. A 3x3 HT/FT probability matrix and exactly two ranked HT/FT suggestions whenever the matrix can be calculated. Use formal recommendations first; otherwise fill with the highest-EV observation candidates and show their negative or sub-threshold EV plainly.

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
- 胜平负概率、推荐概率和 EV
- 推荐、来源 URL、关键理由
- 数据质量，以及每个正式推荐相对临场市场的 `aligned/neutral/against/conflicting/unknown` 分类

通过两个 `--exact-score-pick SCORE:PROBABILITY` 保存波胆候选，并让 `--predicted-score` 等于第一候选。只有通过阈值的正式推荐写入 `asian_pick`、`total_pick`、`half_time_pick` 和 `htft_picks`。每次调用必须通过 `--primary-market` 明确唯一主推；脚本把其余合格方向标为 `secondary`。若没有正式方向，显式传 `--primary-market none`。波胆和观察候选不得计入正式准确率或 ROI。滚球或赛后分析不得伪装为赛前预测，也不得计入准确率。

**不存档 = 工作流未完成。**
