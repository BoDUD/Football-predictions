# Unattended runtime and dispatcher

Use this runtime for every archived pre-match prediction. It combines a Windows wake task, a lightweight Codex dispatcher, and match-specific visible tasks.

## Runtime layers

1. The Windows task runs every five minutes and at logon. It synchronizes missing lineup/review registrations, asks both schedulers for due work and cleanup work, writes exact events to `<workspace>/.codex/soccer-predict/outbox`, and starts the verified Codex Store app when work exists.
2. The recurring Codex dispatcher reads only that outbox. It does not open Titan, calculate probabilities, settle bets, or publish match analysis itself.
3. The dispatcher creates one visible Codex task for each lineup or review event. The child task must claim the scheduler lease before doing work.
4. Cleanup events stay in the dispatcher. They verify that the result task already has a completed final answer before marking delivery and removing automations.

Locking the Windows session does not stop this workflow. Powering off, logging out, losing network access, or disabling Windows Task Scheduler still prevents execution. Never claim otherwise.

## Windows task

Install against explicit paths:

```text
powershell -ExecutionPolicy Bypass -File <skill-dir>/scripts/install_windows_watchdog.ps1 install -Workspace <workspace> -PythonPath <python.exe>
```

The task name is `SoccerPredict-Watchdog`. It uses the current interactive user, runs every five minutes and at logon, starts missed runs when available, permits battery execution, requests wake-to-run, ignores overlapping instances, and retries failures three times. Use the script's `status` action to verify `Installed`, `State`, `LastTaskResult`, and `NextRunTime`.

The watchdog dynamically resolves the installed `OpenAI.Codex` AppX AUMID. Package activation only starts Codex; it does not fabricate or consume an analysis.

## Dispatcher contract

On every dispatcher run:

1. Run `soccer_watchdog.py --workspace <workspace> --skill-dir <skill-dir>`.
2. Run it again with `--list-events`.
3. Process at most one event per five-minute run so a later cron tick cannot interrupt a long batch. Prioritize time-sensitive `lineup`, then `review`, then delivery/cleanup, then metadata recovery.
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
- Review title: `复盘｜<league_key>｜<match_id>｜<home_team> vs <away_team>`.
- Resolve the current task ID from `nodeRepl.requestMeta.threadId` before claiming. Refuse to continue if it is absent; never invent or reuse an originating task ID.
- Read the Skill, claim the exact scheduler item, and stop/archive on a refused claim.
- Persist a complete non-empty result artifact and call `complete` with the current task ID and artifact path.
- Send the final answer immediately after `complete`. Do not delete automations or call `mark-delivered` in the result task.
- A later dispatcher run verifies the final answer and performs cleanup.

If a review check finds no explicit finished status, call `review_scheduler.py wait`; archive that no-result check. The scheduler creates exactly one 30-minute follow-up, and the dispatcher later creates a fresh check task.
