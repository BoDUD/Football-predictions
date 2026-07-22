# Codex Post-Match Review

## Inputs

- Match ID, plus either a user-supplied final score or a verified final score from titan007.
- The archived pre-match record in `<workspace>/.codex/soccer-predict/history.json`.

If no archived pre-match record exists, provide an informational review but do not add it to accuracy statistics.

## Procedure

1. Inspect the archived record for the match ID. Treat `status: reviewed` as final. If already reviewed, stop immediately: do not fetch the match again, rerun settlement, rewrite learning notes, create another full review, or schedule another review. Return only a brief already-complete notice with the stored final score and review time when available.
2. Run `memory_store.py pending` and confirm the match is pending.
3. Open the Titan live/detail page and require an explicit finished status. A non-empty score or elapsed clock is insufficient. If the match is prematch, live, at half-time, in extra time, in penalties, delayed, interrupted, postponed, cancelled, or ambiguous, stop without settlement and leave the record pending.
4. Verify the half-time score, final score, and whether the final score is a 90-minute result. Asian and totals markets normally settle on regulation time unless the archived record states otherwise.
5. Note red cards, penalties, disallowed goals, major injuries, or unusual stoppages when available.
6. Resolve the final active pre-match version. Prefer the archived `lineup-check`; use `initial` only when no valid lineup-check exists. Write a specific causal learning and run `memory_store.py review --verified-finished ... --key-learning "..."` with the final home and away scores exactly once.
7. Compare both ranked exact-score candidates with the result, then compare Asian settlement, totals settlement, first-half settlement, HT/FT settlement, and the main causal assumptions.
8. Run `memory_store.py stats`; report global `primary` and its per-market breakdown, then the current match's normalized league profile. Do not grade or report secondary-pick outcomes.
9. Run `memory_store.py calibrate --write` so the workspace calibration snapshot stays synchronized.
10. Run `wechat_formatter.py --base-dir <workspace> --match-id <id> --kind review` and append the resulting plain text as the manual `微信可复制版`. Do not auto-send it.

When the review has its own Codex task, use `复盘｜<league_key>｜<match_id>｜<home_team> vs <away_team>` as the title. Keep one match per task; the normalized league prefix provides the grouping.

## Settlement and statistics

The script handles whole, half, and quarter lines. `half_win` counts as a correct primary direction, `half_loss` as incorrect, and `push` is excluded from the accuracy denominator. Calculate one-unit flat-stake money only for `primary`, using Hong Kong odds: win `+odds`, half-win `+odds/2`, push `0`, half-loss `-0.5`, and loss `-1`. Keep secondary picks as pre-match references only: do not settle them, persist a hit/miss result for them, include them in accuracy, assign them a stake, or calculate profit/ROI. `primary` settles at most one final active main pick per match. `primary_by_market` categorizes those same primary results; `all_formal` remains only as a compatibility alias and must not reintroduce secondaries. A lineup-check supersedes the initial version. Keep the initial snapshot in `revisions` for diagnosis, never for duplicate grading. Persist and display `settlement_basis` so the settled stage and primary are auditable. HT/FT settles only when it is the primary and the verified half-time score exists. Report exact-score Top-1 and Top-2 hit rates only as scenario diagnostics; they never enter primary accuracy. Live analyses and observation candidates never enter the denominator.

## Learning updates

Write a concise, non-empty `key_learning` grounded in observed evidence. Evaluate only the final active primary assumption; do not describe a secondary pick as hit or missed or use its outcome as calibration evidence. Name the primary assumption that was confirmed or rejected; do not use generic text such as “模型需优化”. Do not claim model training occurred merely because prose weights changed. Only describe a parameter as updated when a durable value was actually saved.

After each review, persist the calibration snapshot. Generate its summary from current statistics and lead with `primary`; never reuse an old hand-written match count. Require at least 20 graded selections in a market plus feature-level evidence before changing weights. Before that threshold, keep weights unchanged; save only provisional guardrails and data-quality lessons.

Group reviews by normalized `league_key`, while retaining every original competition label in `source_labels`. Treat season prefixes, round suffixes, and knockout-stage suffixes as metadata rather than separate leagues. Use `league_profiles.<league_key>.recent_learnings` to carry causal lessons into later matches from that league. League samples below 10 reviewed matches are anecdotal; 10-19 are provisional. Never transfer a one-match league pattern into a global weight.

When the user asks for a review-record summary, compare at least:

- Primary accuracy, profit, and ROI overall and by primary market; do not summarize secondary results.
- Archived EV versus realized flat-stake ROI.
- Results grouped by `market_signal` when enough classified records exist.
- Initial versus lineup-check revisions and whether the primary pick changed.
- Missing half-time scores, empty learnings, duplicate revisions, and other data-quality gaps.

Treat fewer than 10 reviewed matches as anecdotal and 10-19 as provisional. Do not infer league-specific skill from a single match.

Useful review questions:

- Did line movement add information or merely follow public money?
- Did the lineup change the pre-match assumptions?
- Did a defensive absence increase goals as expected?
- Was the league scoring baseline calibrated correctly?
- Was the prediction wrong because of an unforeseeable event?

## Automatic review

The Skill cannot wake itself. A Codex automation must be explicitly requested and scheduled. Its prompt must include the match ID and workspace path so the later task reads the same history file.

Every automatic review status check must be one-time. At startup, check the archived status; if it is already `reviewed`, end without another review or rescheduling. If Titan does not explicitly show a terminal match status, do not call the review command. Leave the record pending; when automatic review was authorized, schedule one non-duplicate follow-up status check 30 minutes later. Stop retrying postponed, cancelled, or abandoned matches and report their administrative status without settlement.
