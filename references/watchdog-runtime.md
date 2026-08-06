# One-time runtime and optional watchdog

Use exact per-match Codex automations by default. They run only at the verified T-30 and kickoff-plus-three-hour times and create match-specific visible tasks. Do not keep Python or PowerShell running, and do not install or enable five-minute polling unless the user explicitly opts in.

## Runtime layers

1. After an initial archive, create one T-30 automation and one kickoff-plus-three-hour review automation. Both must end in `COUNT=1`, carry the scheduler's versioned exact-UTC one-time schedule spec, and bind `dtstart_utc`, `until_utc`, and `run_at_utc` to the same instant. If the platform reports a next-run timestamp, attachment succeeds only when it exactly equals `run_at_utc`.
2. Each automation opens one visible Codex task for its named match. The child task must claim the scheduler lease before doing work.
3. The T-30 task must not run early or after kickoff. The review time only makes a check due and is not proof of full-time.
4. A later user-initiated invocation or explicit one-time cleanup task verifies the completed result before marking delivery and removing exact automation references.

Powering off, logging out, losing network access, or making the local Codex execution environment unavailable prevents execution. Never claim otherwise.

## Optional recurring Windows watchdog

The recurring watchdog is a fallback for users who explicitly accept five-minute polling. It is not the default and must never be enabled merely to support one scheduled match.

Install against explicit paths:

```text
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/install_windows_watchdog.ps1 install -AllowRecurring -Workspace <workspace> -PythonPath <python.exe>
```

The task name is `SoccerPredict-Watchdog`. `install` and `enable` refuse to proceed without `-AllowRecurring`. Use `disable` to stop polling without deleting the task. When explicitly enabled, it runs every five minutes and at logon, starts missed runs when available, permits battery execution, requests wake-to-run, ignores overlapping instances, and retries failures three times.

The watchdog dynamically resolves the installed `OpenAI.Codex` AppX AUMID. Package activation only starts Codex; it does not fabricate or consume an analysis.

## Optional dispatcher contract

Only when recurring polling was explicitly enabled:

1. Run `soccer_watchdog.py --workspace <workspace> --skill-dir <skill-dir>`.
2. Run it again with `--list-events`.
3. Process at most one event per run. Prioritize time-sensitive `lineup`, then `review`, then delivery/cleanup, then metadata recovery.
4. If the protected workspace `.codex` state returns `Permission denied` in a Codex sandbox, immediately retry the exact scheduler/watchdog command with controlled escalation. This installation is explicitly authorized to update only the soccer-predict state directory.
5. If no events exist, archive the dispatcher run and end without opening Titan or producing a user notification.
6. For `lineup` and `review` events, create a new task in the saved workspace project. In a project-bound cron run, prefer a same-directory fork followed immediately by the full match-specific prompt; use direct thread creation when it returns reliably. Do not execute the match inside the dispatcher. Acknowledge the event only after a real child thread ID exists, the full prompt was delivered, and the title was set:

```text
python <skill-dir>/scripts/soccer_watchdog.py --workspace <workspace> --ack-event <event_id> --scheduler <scheduler> --thread-id <new_thread_id>
```

7. `await_complete_metadata` is emitted only after a ten-minute grace period, so a normal worker can finish its artifact and final answer without racing a recovery task. Once emitted, create a new saved recovery task before acknowledging the event. The recovery task must rebuild the display from archived state without re-settling, save a non-empty artifact, call the matching `complete` or `terminal` command with its own task ID, and send the result. Never leave this state waiting forever.
8. For cleanup event `verify_delivery`, read the stored result task by its exact `thread_id`. Require task status `completed` and a non-empty final answer. If either check fails, leave the event pending.
9. After verified delivery, run the matching scheduler's `mark-delivered --thread-id <result_thread_id>`. Delete or disable every exact automation reference, then call `mark-cleaned` with the complete confirmed ID set. Acknowledge the cleanup event only after these steps succeed.
10. Archive the dispatcher run after the one selected event is handled. Never archive the child result task.

Moving an outbox event to `processed` records dispatch, not analytical success. Scheduler leases and terminal state remain the source of truth; an unclaimed item is eligible to be queued again.

## Match-specific child tasks

- Lineup title: `临场复查 <match_id>｜<home_team> vs <away_team>`.
- Review title: `复盘｜<competition_display_label>｜<match_id>｜<home_team> vs <away_team>`. Resolve the source-verified Chinese display label through the same path as the card; use a stable Chinese league mapping or `赛事待核验` when evidence is unavailable. Keep ASCII competition/league keys in internal scheduler metadata only.
- Resolve the current task ID from `nodeRepl.requestMeta.threadId` before claiming. Refuse to continue if it is absent; never invent or reuse an originating task ID.
- Read the Skill, claim the exact scheduler item with `--thread-id <current_thread_id>`, and stop/archive on a refused claim.
- Persist a complete non-empty result artifact and call `complete` with the current task ID and artifact path.
- Send the final answer immediately after `complete`. Do not delete automations or call `mark-delivered` in the result task.
- A later user-initiated invocation, explicit one-time cleanup task, or optional dispatcher run verifies the final answer and performs cleanup.

If a review check finds no explicit finished status, call `review_scheduler.py wait --thread-id <current_thread_id>`; archive that no-result check and create exactly one fresh `COUNT=1` follow-up automation for 30 minutes later. Do not start a recurring retry loop.
