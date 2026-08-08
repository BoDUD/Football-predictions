# Codex Post-Match Review

## Inputs

- Match ID, plus either a user-supplied final score or a verified final score from titan007.
- The archived pre-match record in `<workspace>/.codex/soccer-predict/history.json`.

If no archived pre-match record exists, provide an informational review but do not add it to accuracy statistics.

## Procedure

1. Inspect the archived record for the match ID. Treat `status: reviewed` as final. If already reviewed, stop immediately: do not fetch the match again, rerun settlement, rewrite learning notes, create another full review, or schedule another review. Return only a brief already-complete notice with the stored final score and review time when available.
2. Run `memory_store.py pending` and confirm the match is pending.
3. Open the Titan live/detail page and require an explicit finished status. A non-empty score or elapsed clock is insufficient. If the match is prematch, live, at half-time, in extra time, in penalties, delayed, interrupted, postponed, cancelled, or ambiguous, stop without settlement and leave the record pending.
4. Verify the final score and whether it is a 90-minute result; also verify the half-time score when available. Record the verification URL/provider and a timezone-aware collection time. Football markets normally settle on regulation time unless the archived record states otherwise. A half-time or HT/FT primary requires the verified half-time score and must remain pending without it. For a no-primary or full-time primary review, unavailable half-time evidence is recorded as missing and any related observation remains ungraded; never infer it from the final score. If the final active primary is a corner market, also verify both official 90-minute corner counts.
5. Note red cards, penalties, disallowed goals, major injuries, or unusual stoppages when available.
6. Resolve the final active pre-match version. Prefer the archived `lineup-check`; use `initial` only when no valid lineup-check exists. Write a specific causal learning and run `memory_store.py review --verified-finished ... --verification-source ... --verification-collected-at ... --key-learning "..."` with non-negative final home and away scores exactly once. Add both half-time scores for a half-time/HTFT primary and both `--home-corners` and `--away-corners` for a corner primary; if a required pair is unavailable, do not review and leave the record pending.
7. Compare both ranked exact-score candidates with the result, then compare Asian settlement, totals settlement, first-half settlement, HT/FT settlement, and the main causal assumptions.
8. Run `memory_store.py stats`; lead with `strict_oos` primary performance and its per-market breakdown. Report legacy/backfill/invalid-timing cohorts separately, then the current match's normalized league profile. If the final active version had no primary, report it only as a no-primary learning sample and verify the primary record did not change. Do not grade or report secondary-pick outcomes. For 3.13+ records, also verify the frozen mandatory evaluation primary, its separate non-monetary settlement, and the content-hashed `review-training-sample/1.0.0`; never merge evaluation accuracy with strict betting accuracy/ROI.
9. Run `memory_store.py calibrate --write` so the workspace calibration snapshot stays synchronized.
10. Run `plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review` and preserve its required information once in the normal review text. Append the exact result under `可复制纯文本版` only when the user explicitly requests a copyable block or text-only mode. Preserve the primary or explicit no-primary line, score references, causal learning, league record, and cumulative record. The project has no external-message sender.

When the review has its own Codex task, use `复盘｜<competition_display_label>｜<match_id>｜<home_team> vs <away_team>` as the title. Resolve the source-verified Chinese competition label through the same display path used by the card. If verified competition evidence is unavailable, use the stable Chinese league mapping or `赛事待核验`; never expose an ASCII proxy-model `league_key` or `competition_key` in the user-visible title. Keep one match per task. Internal keys still provide scheduler/statistical grouping.

## Settlement and statistics

The script handles whole, half, and quarter lines. `half_win` counts as a correct primary direction, `half_loss` as incorrect, and `push` is excluded from the accuracy denominator. Calculate one-unit flat-stake money only for `primary`. Hong Kong odds are net profit; decimal winning odds convert to `odds - 1`. Keep secondary picks as pre-match references only: do not settle them, persist a hit/miss result for them, include them in accuracy, assign them a stake, or calculate profit/ROI. `primary` settles at most one final active main pick per match. A reviewed match with no final active primary contributes zero record matches, stake, profit, and ROI; persist it as `learning_scope: no_primary_observation` and `counts_toward_primary_record: false`, and include its causal note in learning samples. A lineup-check supersedes the initial version. Preserve every accepted revision append-only and freeze the settled basis. Reject negative scores and incomplete required evidence. Public review output reports only the frozen global joint-event Top 2, its Rank-1-derived `联合首选情景总球`, the separately labelled audit-only `总进球边际第一`, and the Top-2/remaining mass plus versioned entropy uncertainty recomputed from the complete frozen joint distribution. The marginal leader never replaces or reranks a joint event. Independent exact-score and HT/FT selections, hit ranks, and grading remain machine-only diagnostics. Only records generated and archived before kickoff with strict market/model provenance enter `strict_oos`; quarantined cohorts remain available for forensic learning but never headline performance.

## Learning updates

Every 3.13+ review freezes all v3 candidate distributions and their available observed settlement states, not only the selected direction. This sample cannot update the active model. After the cohort closes, export a non-overwriting next-version bundle with `python -m scripts.review_training_export --base-dir <workspace> --cohort-id <closed-id> --output <new-staging-path>.json`. Missing or active closures, result-evidence drift, self-rehashed sample changes, duplicate fixtures, and existing outputs fail closed. Old reviews are not backfilled.

Write a concise, non-empty `key_learning` grounded in observed evidence. When a primary exists, evaluate only its final active causal assumption. When no primary exists, evaluate why the best observation stayed below the gate and what the verified result teaches about abstention thresholds, data quality, or model tail risk; never relabel that observation as a winning or losing bet. Do not describe a secondary pick as hit or missed or use its outcome as calibration evidence. Do not use generic text such as “模型需优化”. Do not claim model training occurred merely because prose weights changed. Only describe a parameter as updated when a durable value was actually saved.

After each review, persist the calibration snapshot. Generate its summary from current statistics and lead with `strict_oos`; never reuse an old hand-written match count. Twenty graded strict-OOS selections in one market is only a minimum review trigger, not authority to change a parameter. A parameter or market-policy change additionally requires clean walk-forward proper-score and calibration evidence under [model-validation.md](model-validation.md). `calibrate --write` records evidence and guardrails; it does not fit the score model.

Freeze the source-bound competition evidence, `competition_key`, raw league, and normalized `league_key` into `settlement_basis` before grading. Group reviews by that frozen `competition_key` when validated evidence exists; otherwise use the frozen normalized `league_key`. Retain every original competition label in `source_labels`, and never use a proxy model key as the user-facing competition name. Treat season prefixes and round suffixes as display metadata; treat a genuinely different registered competition, such as a domestic cup versus a league using its model as a proxy, as a separate review cohort. Use the matching internal profile key's `recent_learnings` to carry causal lessons into later matches from that competition. Samples below 10 reviewed matches are anecdotal; 10-19 are provisional. Never turn a one-match pattern into a model or policy change.

When the user asks for a review-record summary, compare at least:

- Strict-OOS primary accuracy, profit, ROI, coverage, and abstention overall and by primary market; show quarantined cohorts separately.
- Archived EV versus realized flat-stake ROI.
- 1X2 log loss/Brier score, probability-bin calibration, and model-version sample sizes whenever the prediction artifacts are available.
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

For this installation, every successfully archived pre-match prediction is idempotently registered with `review_scheduler.py`. The first check is due at verified kickoff plus three hours in `Asia/Tokyo`. One exact `COUNT=1` Codex automation provides execution; its versioned schedule spec binds `run_at_utc`, `dtstart_utc`, and `until_utc` to the same instant. When the platform reports next-run, pass it to `attach-automation --platform-next-run`; reject any mismatch rather than accepting a next-day recurrence. Do not run a global polling watchdog. See [watchdog-runtime.md](watchdog-runtime.md).

Every due event creates a new saved review task. That task must resolve its saved task ID and claim the exact scheduler item with `--thread-id <current_thread_id>` before opening Titan. At startup, check the archived status; if it is already `reviewed`, end without another review. If Titan does not explicitly show a terminal match status, do not call the review command: call `review_scheduler.py wait --thread-id <current_thread_id>`, leave the record pending, archive that no-result task, and create exactly one fresh `COUNT=1` follow-up automation for 30 minutes later. Do not start a recurring retry loop. Stop retrying postponed, cancelled, or abandoned matches and report their administrative status without settlement.

After a verified review, save the complete user-facing result artifact and call:

```text
python <skill-dir>/scripts/review_scheduler.py --base-dir <workspace> complete --match-id <id> --thread-id <current_thread_id> --result-artifact <path>
```

Send the final answer immediately. The result task must not call `mark-delivered`, delete automations, call `mark-cleaned`, or archive itself. A later user-initiated invocation or explicit one-time cleanup task verifies the exact task is completed with a non-empty final answer, marks delivery, removes only exact automation references, and marks cleanup. Do not keep a polling process alive for cleanup.
