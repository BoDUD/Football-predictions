---
name: soccer-predict
description: >
  Analyze football matches from titan007.com using Asian handicap, totals, European odds,
  first-half and half-time/full-time markets, fundamentals, lineups, corners, and post-match review. Use for match IDs or descriptions,
  football predictions, handicap/over-under analysis, result reviews, accuracy statistics,
  automatic checks around 30 minutes before kickoff, and requests to schedule an automatic Codex
  review after a match.
---

# Soccer Predict for Codex

Run three workflows: collect data, predict, then review. Treat all probabilities and EV values as estimates, never guarantees.

## Before analysis

1. Open `https://zq.titan007.com/analysis/{match_id}cn.htm` with the Browser skill.
2. Determine the state from the page: `prematch`, `live`, or `finished`.
3. Resolve the source timezone and the user's local timezone before comparing wall-clock time or scheduling any task. Read the mandatory timezone rules below.
4. Never mix live odds with pre-match odds. Only `prematch` predictions enter accuracy statistics.
5. Read [references/data-collection.md](references/data-collection.md) for collection details.

If the match is already live, label the output `滚球分析`, omit pre-match EV claims, and do not archive it as a pending prediction.

## Timezone resolution (mandatory)

- Treat the time printed on Titan007 Chinese match pages as source data, not automatically as the user's local time. Unless the page exposes a different timezone or direct evidence contradicts it, use `Asia/Shanghai` (`UTC+8`) as the source timezone for Titan007 Chinese pages.
- Read the user's timezone from the Codex environment. Convert the source kickoff to that timezone before computing time-to-kickoff, displaying the local kickoff, archiving `--kickoff`, or creating an automation. Example: Titan `18:30` in `UTC+8` is `19:30` for a user in `Asia/Tokyo` (`UTC+9`).
- Track three explicit values during analysis: `source_kickoff`, `source_timezone`, and `user_local_kickoff`. When the offsets differ, show both source time and user-local time in the match card.
- Cross-check the conversion against the countdown on `live.titan007.com/detail/<match_id>cn.htm` when available. If the converted kickoff and countdown materially conflict, mark the timezone uncertain, do not schedule an automation, and resolve the conflict before claiming a lineup window.
- Never classify a match as live or finished from wall-clock comparison alone. Require the page's explicit status (`VS`, `未开场`, running clock, `完场`, etc.). Wall-clock time controls only scheduling after the timezone conversion is verified.
- Store kickoff timestamps with an explicit offset. Never attach the user's offset to an unconverted Titan source time.

## Predict

Read [references/prediction-framework.md](references/prediction-framework.md) and [references/half-time-full-time.md](references/half-time-full-time.md), then:

- Before calculating a new prediction, run `memory_store.py stats` and read `<workspace>/.codex/soccer-predict/calibration.json` when it exists. Apply its guardrails, but apply weight overrides only when `weight_change_eligible` permits them and the stored adjustment is supported by feature-level evidence.
- Default to `可视化模式` for every prediction and review unless the user explicitly asks for `简洁模式`, `简洁`, `concise`, or `short`.
- Visual mode must use compact Markdown tables and probability bars to show match status, opening-to-current Asian handicap and totals movement, no-vig 1X2 probabilities, EV comparison, key fundamentals/lineups, recommendations, predicted score, and risks.
- Visual mode must also show a first-half panel and a 3x3 HT/FT probability matrix. Show at most one first-half pick and **exactly two ranked HT/FT suggestions** whenever the model matrix is available.
- Classify each HT/FT suggestion as either `正式推荐` or `观察候选（未达标）`. A formal recommendation must pass the thresholds in `references/half-time-full-time.md`. If fewer than two outcomes pass, fill the remaining slots with the highest-EV outcomes and label them as observation candidates. Never hide the two ranked suggestions behind a generic “观望” result, and never describe a negative-EV candidate as positive value.
- For both HT/FT suggestions show selection, current odds, model probability, no-vig market probability, model-versus-market edge, EV, rank, and status. When current odds are unavailable, rank by model probability and label the suggestion `赔率缺失，不可执行`; never invent odds or EV.
- Concise mode: return only the best direction, probability, EV, predicted score, and one short rationale.
- If some visual fields are unavailable, keep the section visible and mark them `数据未取得` or `待公布`; never invent values to fill the layout.
- Use current pre-match odds for final calculations and opening odds only for movement analysis.
- Handle quarter lines with their real half-win/half-loss settlement; do not reduce them to a binary outcome.
- State when lineups are unconfirmed or data is incomplete.

### Full-time recommendation gate

- Classify Asian handicap and total-goals directions as `正式推荐` or `观察候选（未达标）`.
- A normal formal recommendation requires current EV of at least 5%, a comparable model-versus-no-vig market edge of at least 3 percentage points, and medium or high data quality.
- If the current consensus line and 1X2/total market both move materially against the selection, require EV of at least 8%, edge of at least 4 percentage points, data from at least five firms, and independent lineup or fundamental evidence. This gate also applies to the primary pick; otherwise downgrade it to observation even if the raw EV is positive.
- This adverse-movement gate is a provisional guardrail supported by the current review sample. Reassess it as the sample grows; do not hard-code a new global model weight from fewer than 20 graded selections in that market.
- Show the highest-ranked observation when no direction qualifies, but label it `观察/不下注`; never call it `主推`.

### Mandatory Codex archive

After every completed **pre-match** prediction, run:

```text
python <skill-dir>/scripts/memory_store.py record --analysis-stage initial [fields...]
```

The script stores records under `<workspace>/.codex/soccer-predict/history.json`. Pass `--base-dir <workspace>` when the current directory is not the intended workspace. Do not use `.openclaw` or `.claude` paths.

Include the match ID, league, kickoff, teams, predicted score, 1X2 probabilities, source URL, concise rationale, `--data-quality`, and the market-signal classification for each archived pick: `aligned`, `neutral`, `against`, `conflicting`, or `unknown`. Archive only formal Asian, total, first-half, and HT/FT recommendations in their pick fields; keep unqualified observations in notes so they do not pollute accuracy or ROI. Use `--asian-market-signal`, `--total-market-signal`, and `--half-market-signal` when the corresponding formal pick exists. Archive HT/FT recommendations with `--htft-pick SELECTION:ODDS:PROBABILITY:EV`. If archiving fails, report the failure instead of claiming learning is enabled.

Pass `--primary-market` on every `record` call. Select exactly one archived formal direction as the machine-readable primary; the script marks every other formal direction `secondary`. Use `--primary-market none` only when no formal direction qualifies. A lineup check must explicitly persist `primary_change.status` as `maintained` or `changed`.

The archive command is idempotent for identical predictions. If it returns `duplicate_ignored: true`, do not claim a new revision was created.

## Automatic lineup-time reanalysis in Codex

For every archived prediction whose converted user-local kickoff includes a reliable timezone, create exactly one match-specific Codex automation for **kickoff minus 30 minutes**. Do not run the lineup reanalysis earlier than T-30. Derive the schedule from the verified absolute kickoff instant, not from Titan's displayed hour copied into the user's timezone.

Never create or keep a recurring polling automation for lineup-time reanalysis. Name the one-time task `Soccer Predict 临场复查 <match_id>` and check for that exact task before creating it so retries do not make duplicates.

The automation must:

1. Process only its named match ID; do not scan every pending match.
2. Reopen the titan007 match page and collect current Asian handicap, totals, 1X2, first-half odds, HT/FT odds, confirmed lineups, injuries, and match status.
3. Compare the new data with the archived opening/earlier prediction and produce visual mode output. Highlight changed full-time and first-half lines, changed EV, lineup effects, HT/FT matrix changes, and whether any primary pick changed.
4. If the match is still prematch, archive the revised prediction with `record --analysis-stage lineup-check`. This replaces the active prediction used for settlement while preserving earlier revisions.
5. If the match has started, label it live and do not overwrite the archived pre-match prediction.
6. End after this single run; do not schedule another lineup check.

Delivery is mandatory. The lineup-time automation must finish with a user-facing final answer in its own Codex task, even when no odds, lineup, EV, or recommendation changed. Never treat an archive write, an unchanged result, or an analysis embedded only in the initial prediction as successful delivery. The final answer must begin with `临场复查 <match_id>`, state the check time and match status, and show `主推维持` or `主推变更`. When creating the automation, keep successful-run notifications enabled and tell the user the scheduled local time.

Thread separation is mandatory:

- Keep the originating analysis task for the initial prediction only. Do not post the lineup-time result back into that task.
- Run every scheduled lineup-time automation as a new standalone Codex task and keep it visible in the task list. Use the title `临场复查 <match_id>｜<home_team> vs <away_team>`.
- When an immediate lineup-time check is required instead of a scheduled automation, locate the Codex thread-creation tool and create a new project task for the check. Return only the new-task confirmation in the originating task; put the reanalysis itself in the new task.
- If the thread-creation tool is unavailable, report that a separate lineup-check task could not be created. Do not silently fall back to publishing the reanalysis in the originating task.
- A user request made inside the originating task to perform the lineup-time check still creates a new task. Follow-up discussion may continue inside that new lineup-check task.

Do not silently count an initial prediction as the delivered lineup-time reanalysis. Apply these timing rules:

- If more than 30 minutes remain, return the initial prediction now and create the distinct one-time automation for T-30; do not run the check early, including after an explicit request.
- If T-30 is less than two minutes away, create a new Codex task and run the lineup-time reanalysis there immediately.
- If fewer than 30 minutes remain but the match is still prematch, create a new Codex task, run the lineup-time reanalysis there immediately, and label it late.
- If the user explicitly requests a lineup-time check before T-30, schedule it for T-30. A requested check at or after T-30 may run immediately and count as the one check, using the required `临场复查 <match_id>` output and delivery format.
- If kickoff time or timezone is uncertain, do not guess the schedule; report that automatic reanalysis could not be scheduled.

Do not mark a lineup-time check complete until data collection and revised analysis succeed.

## Review

Read [references/review-framework.md](references/review-framework.md). A review may be triggered by:

- `复盘 <match_id>`
- `比分 <home>-<away>` when the match is unambiguous
- an automation prompt containing the match ID

Treat `status: reviewed` as a terminal state. Before fetching scores or generating a review, inspect the archived record. If the match is already reviewed, do not fetch data, run settlement again, rewrite the record, produce another full review, or schedule another review. Return only a brief notice that the review is already complete, together with the stored final score and review time when available.

Apply a hard terminal-state gate before every manual or automatic review:

- Open the Titan live/detail page and require an explicit terminal status such as `完`, `完场`, or `Finished`. A visible score, a 90+ minute clock, half-time, extra time in progress, or penalties in progress is not proof that the match has ended.
- If the page says `未`, `进行中`, `中场`, `加时中`, `点球中`, `推迟`, `中断`, `取消`, or the status is missing/conflicting, do not review and do not call `memory_store.py review`.
- When the terminal state cannot be verified, return `比赛未确认完场，暂不复盘`, leave the archive `pending`, and preserve all prediction fields.
- A user-supplied score may bypass the page check only when the user explicitly states that it is the completed final score. For automatic review, page verification is always mandatory.
- For cup matches that finish after extra time or penalties, wait until the whole match is terminal, then settle normal Asian/total markets using the verified 90-minute score unless the archived market explicitly includes extra time.

Fetch the verified final score from titan007 when the user does not supply it. Only after the terminal-state gate passes, run:

```text
python <skill-dir>/scripts/memory_store.py review --verified-finished --match-id ... --half-home-score ... --half-away-score ... --home-score ... --away-score ... --key-learning "..."
python <skill-dir>/scripts/memory_store.py stats
python <skill-dir>/scripts/memory_store.py calibrate --write
```

Only archived pre-match formal recommendations affect accuracy and flat-stake ROI. Explain score error, Asian result, totals result, first-half result, HT/FT results, key miss/hit, cumulative statistics, and the saved learning. If the half-time score cannot be verified, leave half-time and HT/FT picks ungraded.

When reporting `战绩`, `准确率`, or `ROI`, lead with `stats.primary`: one final active primary per match. Report `stats.all_formal` only as secondary detail; never present the combined formal-direction count as the number of match primaries. Exact-score accuracy is diagnostic only and never enters primary accuracy.

`--key-learning` is mandatory and must identify the causal assumption that was confirmed or rejected. Do not use generic text such as “模型需优化”. The calibration snapshot is durable workspace memory; it summarizes accuracy, ROI, market-signal splits, and whether the sample is large enough for weight changes.

Do not automatically change global weights from a tiny sample. Require at least 20 graded selections in the affected market plus feature-level evidence before saving a weight override. With fewer samples, keep weights unchanged and save only provisional guardrails or data-quality lessons.

## Automatic review in Codex

Before creating or running a review automation, inspect the archived record. If its status is already `reviewed`, stop; do not create, rerun, or reschedule a review. Every automatic run must pass the terminal-state gate above before settlement.

### No idle polling

- Never create, retain, or run a recurring global review automation that scans every pending record on an interval.
- Create a one-time review task only for a specific archived match when the user explicitly requests automatic review. Its first check is normally kickoff plus three hours.
- At the start of any automatic task, run `memory_store.py pending`. If it returns no records, end immediately: do not open Titan007, call `review`, `stats`, or `calibrate`, create a follow-up, or emit a substantive review.
- After a specific match is reviewed, cancelled, postponed, or otherwise no longer eligible, disable or delete that match's one-time task. Do not keep a dormant schedule for future unspecified matches.

Do not claim that the Skill wakes itself. When the user says `自动复盘`, `预测并自动复盘`, or explicitly asks for automation:

1. Complete and archive the pre-match prediction.
2. Locate the Codex automation tool.
3. Create a one-time status-check task for a reasonable expected completion time, normally kickoff plus 3 hours. Treat this time only as when to check, never as proof of full-time.
4. Put the match ID, workspace path, and instructions to verify an explicit Titan terminal status before calling `review --verified-finished` in the task prompt.
5. If kickoff time or timezone is uncertain, ask before scheduling.

If an automatic status check finds the match still running, delayed, in extra time, in penalties, or otherwise non-terminal, leave the record pending and do not generate a post-match review. When the original request authorized automatic review, schedule exactly one follow-up status check 30 minutes later and repeat the same gate. Avoid duplicate follow-ups. For postponed, cancelled, or abandoned matches, stop retrying and report the terminal administrative status without settling the prediction.

Do not create an automation without an explicit user request.

## Local data commands

```text
python <skill-dir>/scripts/memory_store.py pending
python <skill-dir>/scripts/memory_store.py stats
```

Use these before answering questions about pending reviews or historical accuracy.
