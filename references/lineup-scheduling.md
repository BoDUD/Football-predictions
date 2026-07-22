# Retry-safe lineup scheduling

Use this workflow only for pre-match lineup reanalysis. It creates one logical T-30 check with bounded recovery attempts; only the attempt that obtains the lease may analyze or archive.

## Register after the initial archive

Run:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> register --match-id <id> --source-timezone Asia/Shanghai --user-timezone Asia/Tokyo
```

Require an archived kickoff with an explicit offset. Verify that the returned `source_kickoff`, `kickoff`, and `scheduled_for` represent the same absolute instant and that the user-local values use `+09:00`. Stop if Titan's countdown conflicts with that conversion.

Use the returned `retry_plan`: T-30 is the primary attempt, followed by bounded one-time recovery attempts at T-25, T-20, T-15, T-10, T-5, and T-2. These attempts are resilience against a temporarily unavailable Codex executor, not seven analyses. Never schedule a retry before T-30 or at/after kickoff, and never replace this plan with a global polling task.

Create each attempt as a standalone local Codex automation in the same saved project. Use these names:

- Primary: `Soccer Predict 临场复查 <match_id>`
- Recovery: `Soccer Predict 临场复查 <match_id> 补跑 T-<minutes>`

Check exact names before creation. After each creation, persist its returned ID:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> attach-automation --match-id <id> --automation-id <automation_id> --automation-name "<name>"
```

Tell the user the primary Japan-time check time. Keep successful delivery enabled because a recovery attempt may become the real analysis.

## Automation prompt contract

Give every attempt the same match-specific instructions:

1. Read the soccer-predict Skill and process only the named match ID.
2. Run `lineup_scheduler.py claim --match-id <id>`. Do not open Titan or calculate anything unless `claimed` is `true`.
3. If the claim is refused because another lease is active, the revision is complete, or the task is terminal, delete/disable this attempt and archive its no-op Codex task.
4. When claimed, rename the current standalone task to `临场复查 <match_id>｜<home_team> vs <away_team>` and reopen Titan. Require the page's explicit match status.
5. If still pre-match, collect current Asian, totals, 1X2, first-half, HT/FT, confirmed lineups, and injuries. Compare them with the active archived prediction and produce visual mode output.
6. Archive the result using `record --analysis-stage lineup-check`; preserve revisions and explicitly report whether the primary is maintained or changed.
7. Run `lineup_scheduler.py complete --match-id <id> --thread-id <current_thread_id>`. Completion must fail if the lineup revision was not archived.
8. Delete or disable every automation in `cleanup_automation_refs`, then pass the deleted IDs to `mark-cleaned`.
9. If collection or archival fails while the page is still pre-match, run `release --reason <concise cause>` so the next bounded retry can claim. Keep the failure task visible and state that recovery remains scheduled.
10. If the page explicitly says started, finished, cancelled, or postponed, do not overwrite the pre-match archive. Run `terminal --reason <state>`, clean all attached automations, and report the state.

The claimed task's final answer must be delivered even when no recommendation changes. Begin with `临场复查 <match_id>`, include the Japan-time check time, and show `主推维持` or `主推变更`.

## Opportunistic catch-up

At the beginning of every soccer-predict invocation, run:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> due
```

For each returned item, create a new project task immediately. The new task must follow the automation prompt contract and obtain the claim itself. Do not analyze the due match in the originating task. `due` returns only tasks between T-30 and kickoff, so a recovered executor can make up a missed T-30 check without creating a post-kickoff pseudo-check.

An executor that remains offline cannot run code while it is offline. The bounded attempts and invocation-time catch-up ensure the next available pre-match execution opportunity can recover; they must never fabricate a check after kickoff.

## Cleanup and audit

Use `status --match-id <id>` to inspect schedule, attempts, leases, terminal state, attached automations, and cleanup confirmation. A stale lease expires automatically. Use `release` only for retryable execution failures; use `terminal` for explicit match states.

After successful archive or a terminal match state, remove all attached automations and run:

```text
python <skill-dir>/scripts/lineup_scheduler.py --base-dir <workspace> mark-cleaned --match-id <id> --automation-id <id> [--automation-id <id> ...]
```

Do not leave past one-time tasks marked active. Do not mark cleanup complete until the automation tool confirms deletion or disablement.
