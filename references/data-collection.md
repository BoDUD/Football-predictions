# Data Collection Guide

## Table of Contents
1. [Data Source](#data-source)
2. [Input Formats](#input-formats)
3. [Data Points to Extract](#data-points-to-extract)
4. [Extraction Procedure](#extraction-procedure)

## Data Source

Base URL: `https://zq.titan007.com/analysis/{match_id}cn.htm`

Use 新球体育 (XinQiu Sports) data provider on the page.

## Input Formats

Accept either format from user:
- **Match ID only**: e.g. `2908467` -> construct URL `https://zq.titan007.com/analysis/2908467cn.htm`
- **Match description**: e.g. `2026.3.15 09:30 美职业 皇家盐湖城vs奥斯丁` -> search or confirm match ID with user

## Data Points to Extract

### 0. Timezone and Match State

- Record the kickoff string exactly as Titan displays it.
- Record the source timezone separately. For Titan007 Chinese pages, use `Asia/Shanghai` (`UTC+8`) unless Titan itself provides an explicit different timezone or absolute timestamp.
- Read the user's timezone from the Codex environment and convert the kickoff to the user's local time before scheduling or calculating time remaining.
- Preserve all four machine-readable values: source kickoff, source timezone, converted user-local kickoff, and user timezone. Store the page's explicit status separately.
- Use the live-detail countdown only as a non-authoritative sanity check; it may parse a timezone-less wall time in the browser timezone and cannot by itself override the Chinese-page `Asia/Shanghai` default.
- Keep page status and clock math separate: use explicit page status to classify `prematch`, `live`, or `finished`; use the converted clock only to determine the lineup-check window.
- A formal archive requires `page_status=prematch`, a timezone-aware source collection time, and an archive time strictly before kickoff. Reject ambiguous, offset-free, at-kickoff, or post-kickoff snapshots rather than repairing them later.

### 1. Asian Handicap (让球盘)
- Extract only rows labeled "即" (instant/live) and "早" (early/opening)
- Fields: home odds, handicap line, away odds
- Note which team is giving the handicap
- Record all available bookmaker data
- For the selected current line, preserve both sides from the same collection window, odds format, market completeness, source URL, timestamp, bookmaker count, and consensus/median price basis.

### 2. Over/Under (大小球盘)
- Extract only rows labeled "即" (instant/live) and "早" (early/opening)
- Fields: over odds, total goals line, under odds
- Record all available bookmaker data
- For the selected current line, preserve both sides and the same audit metadata required for Asian handicap.

### Goal Range and Both Teams to Score

- Collect the complete current goal-range outcome set, such as `0-1`, `2-3`, `4-6`, and `7+`, when a verified source publishes it.
- Collect both BTTS outcomes, Yes and No, from the same source and timestamp.
- Record the exact outcome labels, odds format, source URL, timezone-aware collection time, bookmaker count, consensus/median price basis, and selected no-vig probability.
- Titan's historical BTTS percentage is supporting team data, not a current market price.
- If Titan does not expose these current prices, use a user-supplied platform screenshot or another verifiable source. Without a complete outcome set, keep the model result as observation only.

### Corner Total and Corner Handicap

- Open `https://m.titan007.com/corner/{match_id}.htm`.
- Collect corner totals from `https://m.titan007.com/HandicapDataInterface.ashx?scheid={match_id}&type=1&oddskind=3&isHalf=0`.
- Collect corner handicaps from the same endpoint with `oddskind=2`.
- Extract both sides of each selected line, opening and current prices when available, bookmaker name, timezone-aware source time, consensus/median price basis, selected no-vig probability, and market status.
- Build and archive the selected side's full-win, half-win, push, half-loss, and loss probability distribution so quarter-line EV is auditable.
- Use at least three bookmakers for a formal corner direction. Do not treat ordinary football totals or handicap prices as corner prices.

### 3. European Odds (欧赔胜平负)
- Extract only rows labeled "即" (instant/live) and "早" (early/opening)
- Fields: home win odds, draw odds, away win odds
- Record all available bookmaker data (at least top 10)
- Preserve the complete current H/D/A set from one collection window and verify its no-vig probabilities sum to one.

### Exact score
- Collect the current exact-score market when Titan publishes it, including the 0-0 row.
- A missing 0-0 market price does not remove 0-0 from the model distribution; record the odds as unavailable and still calculate its probability and rank.
- Never substitute a totals or 1X2 price for an exact-score price.

### First-Half and Half-Time/Full-Time Markets
- Extract opening and current first-half 1X2, Asian handicap, and totals odds.
- Extract all available half-time/full-time outcomes (HH, HD, HA, DH, DD, DA, AH, AD, AA).
- Record at least five bookmakers when possible and keep first-half/HTFT odds separate from full-time odds.
- Collect recent half-time scores, first-half draw and 0-0 rates, first-half goal timing, shots, and corners.
- If Titan does not publish a market, mark it unavailable; never reconstruct its odds from the full-time market.

### 4. Team Fundamentals & History (基本面信息)

#### Core Fundamentals (核心基本面)
- Recent form (last 5-10 matches: W/D/L, goals scored/conceded)
- Home/away performance (home win rate / away win rate)
- Season total goals scored / conceded (and per-game average)
- Home/away goals scored / conceded separately
- Last 10 same-venue matches (home team: last 10 home; away team: last 10 away)

#### Head-to-Head History (历史交锋)
- All available historical matches between these two teams
- Recent 3-5 years of H2H records: W/D/L, goal trends
- Average goals in H2H matches

#### League Standings (联赛排名)
- Current league table position for both teams
- Points gap between teams
- Games played difference

#### Match Importance (比赛重要性)
- Both teams' motivation for this match
- Relegation battle / title race / playoff implications
- Recent scheduling (double headers, fatigue factors)

### 5. Lineups & Detailed Data (首发阵容与详细数据)

#### IMPORTANT: Lineup Timing
- **Note**: Starting lineups are typically published 30-60 minutes before match kickoff
- If collecting data earlier than this window, mark lineup data as "not yet available"
- Re-check for lineups closer to kickoff time if user requests update
- If lineup not available, proceed with prediction using available squad depth info from bench

#### When Available, Extract:
- Starting XI for both teams
- Key player stats (goals/assists this season)
- Injury/suspension list (especially core players - impact assessment)
- Bench strength / notable substitutes

#### Enhanced Data for Over/Under (大小球增强)
- Half-time goals data (半场进球数/模式)
- Corner kicks statistics (角球数据)
- Goal difference distribution (净胜球分布: 净胜2+/1/0/-1/-2+)

## Extraction Procedure

1. Navigate to the match analysis page and extract its explicit status without inferring status from wall-clock time alone
2. Extract basic match info (teams, league, displayed kickoff, venue, weather), resolve source/user timezones, and cross-check the converted kickoff against the live-detail countdown
3. Extract Asian handicap data from the "亚让" tab/section
4. Extract over/under data from the "进球数" tab/section
5. Extract European odds from the "胜平负" tab/section
6. Extract goal-range and BTTS markets from a verified complete market when available
7. Extract corner-total and corner-handicap markets plus independent corner-profile data
8. Extract first-half and half-time/full-time markets and half-time scoring data when available
9. Extract team fundamentals from the main analysis page
10. If available near match time, extract lineup data from the lineup section
11. If lineup not yet published, note this and proceed with available data
12. Compile all data into a structured format before proceeding to prediction

Do not calculate formal EV from a display screenshot that omits the opposite side, odds
format, collection time, or market identity. Pass every complete outcome price to the
archive command so it can reproduce and persist the no-vig transformation.

If the page shows a running clock, half-time, full-time, or a non-empty score, do not treat live odds as the final pre-match odds. Label the result as live analysis and exclude it from archived pre-match accuracy.

### Browser Navigation Tips
- The page may have multiple tabs for different data sections
- Use `browser act` to click between tabs if needed
- Use `browser console exec` with JavaScript to extract table data
- If data is loaded dynamically, wait for page to fully render before extraction

### Tabs to Navigate
- Main page: Team fundamentals, recent form, H2H
- 亚让 (Asian Handicap): Asian handicap odds
- 进球数 (Goals): Over/under odds
- 胜平负 (1X2): European odds
- 角球 (Corners): Corner kick data (if available)
- 阵容 (Lineups): Starting XI (timing-dependent)
