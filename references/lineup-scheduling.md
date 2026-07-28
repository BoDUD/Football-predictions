# Retry-safe lineup scheduling

Use this workflow only for pre-match lineup reanalysis. It creates one logical T-30 check; only the child task that obtains the lease may analyze or archive. Read [watchdog-runtime.md](watchdog-runtime.md) for one-time execution and cleanup.

## Register after the initial archive

Run:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> register --match-id <id> --source-timezone Asia/Shanghai --user-timezone Asia/Tokyo
```

Require an archived kickoff with an explicit offset. Verify that the returned `source_kickoff`, `kickoff`, and `scheduled_for` represent the same absolute instant and that the user-local values use `+09:00`. Treat the browser countdown as non-authoritative and never let it change `source_timezone`. For a Titan Chinese-page time of `2026-07-24 06:30`, require `source_kickoff=2026-07-24T06:30:00+08:00`, `kickoff=2026-07-24T07:30:00+09:00`, and `scheduled_for=2026-07-24T07:00:00+09:00`; reject a task that merely relabels `06:30` with `+09:00`.

Also register the post-match review:

```text
python <skill-dir>/scripts/review_scheduler.py --base-dir <workspace> register --match-id <id> --user-timezone Asia/Tokyo
```

Create one exact Codex automation for `scheduled_for`, with an RRULE ending in `COUNT=1`, and attach its real automation ID to the scheduler task. Do not create `retry-T-*` automations: the retry plan is audit/recovery metadata, not a request for repeated execution. `lineup_scheduler.py due` exposes the logical check only from T-30 until kickoff. Do not create a lineup task before T-30 or at/after kickoff, and do not run a global Python or PowerShell polling process.

The one-time automation must create a new saved Codex project task before acknowledging the scheduler item. The child title is:

`临场复查 <match_id>｜<home_team> vs <away_team>`

Tell the user the primary Japan-time check time. Keep successful delivery enabled because a recovery attempt may become the real analysis.

## Automation prompt contract

Give every attempt the same match-specific instructions:

1. Read the soccer-predict Skill and process only the named match ID.
2. Run `lineup_scheduler.py claim --match-id <id>`. Do not open Titan or calculate anything unless `claimed` is `true`.
3. If the claim is refused because another lease is active, the revision is complete, or the task is terminal, delete/disable this attempt and archive its no-op Codex task.
4. When claimed, rename the current standalone task to `临场复查 <match_id>｜<home_team> vs <away_team>` and reopen Titan. Require the page's explicit match status.
5. If still pre-match, collect current Asian, totals, 1X2, first-half, HT/FT, confirmed lineups, and injuries. Compare them with the active archived prediction and produce visual mode output.
6. Archive the result using `record --analysis-stage lineup-check`; preserve revisions and explicitly report whether the primary is maintained or changed.
7. Save the exact complete user-facing result to a non-empty artifact under `<workspace>/.codex/soccer-predict/results/`. Run `lineup_scheduler.py complete --match-id <id> --thread-id <current_thread_id> --result-artifact <path>`. Completion must fail if the revision, task ID, or artifact is missing.
8. Send the final answer immediately in this task. Do not call `mark-delivered`, delete automations, call `mark-cleaned`, or archive this result task.
9. If collection or archival fails while the page is still pre-match, run `release --reason <concise cause>`. A later explicit invocation may catch up before kickoff; do not start a recurring retry loop.
10. If the page explicitly says started, finished, cancelled, or postponed, do not overwrite the pre-match archive. Save a complete terminal notice, run `terminal --reason <state> --thread-id <current_thread_id> --result-artifact <path>`, and send that notice before later cleanup.

Before changing the primary, recalculate both old and new directions from the current market and rerun `stability-v1`; never compare with stale opening-time values. Every replacement needs high data quality, confirmed lineups, an explicit reason, and a new safe Rank 1. If the old primary remains eligible, pass `--previous-primary-current-confidence` and require the new score to be at least five points higher. If hard information makes the old thesis ineligible, pass `--previous-primary-invalidated` and preserve that evidence instead of forcing a score margin. A worse same-direction line also needs `--accept-worse-line`. If the old primary is invalid and no safe candidate qualifies, archive `--primary-market none`. Release a failed claim only when missing data may still arrive before kickoff; never bypass validation.

The claimed task's final answer must be delivered even when no recommendation changes. Begin with `临场复查 <match_id>`, include the Japan-time check time, and show `主推维持` or `主推变更`.

## Opportunistic catch-up

At the beginning of every soccer-predict invocation, run:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> due
```

For each returned item, create a new saved project task immediately and acknowledge it only after the thread tool returns a real ID. The new task must follow the automation prompt contract and obtain the claim itself. Do not analyze the due match in the originating task. `due` returns only tasks between T-30 and kickoff, so a recovered executor can make up a missed T-30 check without creating a post-kickoff pseudo-check.

An executor that remains offline cannot run code while it is offline. The bounded attempts and invocation-time catch-up ensure the next available pre-match execution opportunity can recover; they must never fabricate a check after kickoff.

## Cleanup and audit

Use `status --match-id <id>` to inspect schedule, attempts, leases, terminal state, attached automations, and cleanup confirmation. A stale lease expires automatically. Use `release` only for retryable execution failures; use `terminal` for explicit match states.

The result task stops after its final answer. A later user-initiated soccer-predict invocation or explicit one-time cleanup task uses `cleanup-due`, reads the exact saved result task, and requires both task status `completed` and a non-empty final answer. It then runs:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> mark-delivered --match-id <id> --thread-id <result_thread_id>
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> mark-cleaned --match-id <id> --automation-id <id> [--automation-id <id> ...]
```

If no automation references exist, call `mark-cleaned` without `--automation-id`. Do not delete or disable anything before delivery verification. Do not archive the result task; archive only no-op or explicit cleanup tasks.
