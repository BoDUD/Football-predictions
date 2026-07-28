---
name: soccer-predict
description: >
  Analyze football matches from titan007.com using Asian handicap, totals, goal ranges,
  both-teams-to-score, corner totals and handicaps, European odds, exact scores, first-half
  and half-time/full-time markets, fundamentals, lineups, and post-match review. Use for match IDs or descriptions,
  football predictions, multi-market value analysis, result reviews, accuracy statistics,
  automatic checks around 30 minutes before kickoff, optional guarded delivery of those checks to
  the user's own WeChat conversation, and automatic Codex review scheduling after a match.
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

- Treat the time printed on Titan007 Chinese match pages as source data, not automatically as the user's local time. Use `Asia/Shanghai` (`UTC+8`) unless Titan itself supplies an explicit different timezone or absolute timestamp. A browser-rendered countdown is not evidence of the source timezone.
- Read the user's timezone from the Codex environment. Convert the source kickoff to that timezone before computing time-to-kickoff, displaying the local kickoff, archiving `--kickoff`, or creating an automation. Example: Titan `18:30` in `UTC+8` is `19:30` for a user in `Asia/Tokyo` (`UTC+9`).
- Track three explicit values during analysis: `source_kickoff`, `source_timezone`, and `user_local_kickoff`. When the offsets differ, show both source time and user-local time in the match card.
- Use the countdown on `live.titan007.com/detail/<match_id>cn.htm` only as a non-authoritative sanity check. Its client script may parse a timezone-less wall time in the browser timezone, so never let the countdown alone override `Asia/Shanghai` or relabel the source wall time with the user's offset. If the discrepancy equals the source/user offset, retain the Shanghai conversion and record the countdown as a client-localization artifact; stop scheduling only for a conflict with Titan's own explicit timezone or absolute timestamp.
- Never classify a match as live or finished from wall-clock comparison alone. Require the page's explicit status (`VS`, `未开场`, running clock, `完场`, etc.). Wall-clock time controls only scheduling after the timezone conversion is verified.
- Store kickoff timestamps with an explicit offset. Never attach the user's offset to an unconverted Titan source time.

## Predict

Read [references/prediction-framework.md](references/prediction-framework.md), [references/expanded-markets.md](references/expanded-markets.md), [references/exact-score.md](references/exact-score.md), and [references/half-time-full-time.md](references/half-time-full-time.md), then:

- Before calculating a new prediction, run `memory_store.py stats` and read `<workspace>/.codex/soccer-predict/calibration.json` when it exists. Read the matching normalized `league_profiles` entry as league context. Apply its guardrails, but apply weight overrides only when the relevant sample threshold permits them and the stored adjustment is supported by feature-level evidence.
- Default to `可视化模式` for every prediction and review unless the user explicitly asks for `简洁模式`, `简洁`, `concise`, or `short`.
- Visual mode must use compact Markdown tables and probability bars to show match status, opening-to-current market movement, no-vig probabilities, EV comparison, key fundamentals/lineups, recommendations, predicted score, and risks. Put Asian handicap, totals, goal ranges, BTTS, corner totals, and corner handicaps into one comparable candidate table whenever their current markets are available.
- Visual mode must also show **exactly two ranked exact-score candidates**, a first-half panel, and a 3x3 HT/FT probability matrix. Show at most one first-half pick and **exactly two ranked HT/FT suggestions** whenever the model matrix is available.
- For every valid pre-match or lineup-check prediction, rank the two most probable exact scores from the model score distribution. Label both `高方差参考（不计主推）`; never call them formal picks or include them in primary/all-formal ROI. If exact-score market odds are unavailable, show model probability without inventing odds or EV.
- Always evaluate and archive 0-0 in that same full score distribution. If it ranks first or second, let it appear naturally in the two displayed exact-score candidates. Otherwise keep the audit internal and do not print a separate 0-0 row unless the user explicitly asks for that diagnostic. Odds such as 7.00 never trigger display by themselves. Never force 0-0 into the pair or filter it out of the calculation. Follow `references/exact-score.md` and use the bundled deterministic ranker for a standard Poisson model.
- When a non-Top-2 0-0 audit is hidden, do not repeat its probability, rank, odds, or EV inside recommendation, notes, risks, visual captions, or WeChat copy. Keep those values only in the machine-readable archive.
- Classify each HT/FT suggestion as either `正式推荐` or `观察候选（未达标）`. First validate that the 3x3 matrix reproduces the displayed half-time row marginals and full-time column marginals, then use `scripts/htft_ranker.py` to select exactly two paths by joint model probability. EV only determines whether either selected path clears the formal thresholds; it must never promote a lower-probability long shot into the pair. Never hide the two ranked suggestions behind a generic “观望” result, and never describe a negative-EV candidate as positive value.
- For both HT/FT suggestions show selection, current odds, model probability, no-vig market probability, model-versus-market edge, EV, rank, and status. When current odds are unavailable, rank by model probability and label the suggestion `赔率缺失，不可执行`; never invent odds or EV.
- Concise mode: return only the best direction, probability, EV, exactly two ranked exact scores, and one short rationale. Do not add a separate 0-0 audit unless explicitly requested.
- If some visual fields are unavailable, keep the section visible and mark them `数据未取得` or `待公布`; never invent values to fill the layout.
- Use current pre-match odds for final calculations and opening odds only for movement analysis.
- Handle quarter lines with their real half-win/half-loss settlement; do not reduce them to a binary outcome.
- State when lineups are unconfirmed or data is incomplete.

### Provisional recommendation gate

- Classify every evaluated market direction as `正式推荐` or `观察候选（未达标）`.
- A formal direction needs positive current EV, a positive comparable model-versus-no-vig edge, medium or high data quality, a complete executable current market, and every market-specific evidence check below. Missing or non-positive inputs make it an observation.
- If the current consensus line and related 1X2/total market materially oppose the selection (`against` or materially `conflicting`), require EV >= 8%, edge >= 4 percentage points, at least five firms, and independent confirmed-lineup or fundamental evidence; otherwise keep it as an observation.
- For any formal total-goals direction, additionally require at least five firms plus chance-quality evidence or a confirmed attacking configuration. A price drop, historical over rate, or H2H score pattern cannot satisfy this evidence gate alone.
- For a goal-range or BTTS formal direction, require the complete mutually exclusive market, real current odds, and either chance-quality evidence or an attacking configuration supported by confirmed lineups. Derive its model probability from the same full score matrix used for exact scores; never substitute a totals or exact-score price.
- For a corner-total or corner-handicap formal direction, require a complete two-way current market, at least three firms, independent corner-profile evidence, and a full-win/half-win/push/half-loss/loss probability distribution whose nonzero states are possible at that line. Build a separate corner distribution from home/away corners for and against, width/crossing, dangerous attacks, set pieces, and confirmed personnel; never use football xG as a corner probability proxy.
- Rank only safe formal candidates with the deterministic `stability-v1` score. Prioritize settlement/no-loss safety, then bounded EV and edge strength, data quality, market depth, evidence coverage, and market alignment, with penalties for high variance or conflicting signals. EV 8% and edge 4pp are score saturation references, not ordinary qualification gates. Select exactly one Rank 1 candidate as the primary; do not let raw highest EV or market name override the ranking.
- For every new-market formal direction, archive its no-vig market probability, source, timezone-aware collection time, consensus/median price basis, and complete-market flag. Recalculate edge and EV from those fields; if the supplied values disagree or the recalculated value misses the gate, downgrade it to observation.
- For a selected favorite at `-0.75` or deeper, require high data quality, confirmed lineups, an opponent counterattack/goalkeeper/set-piece tail-risk check, and an independently calculated goal-margin/cover distribution. Support the cover thesis with independent chance-quality evidence; alternatively accept an aligned market from at least five firms only when confirmed attacking configuration and fundamental evidence also agree. Never substitute 1X2 win probability, team reputation, possession, or a strong XI for cover probability.
- Keep these safety and evidence gates active until a feature-level review explicitly changes them. Reaching 20 graded selections does not relax them automatically or authorize a weight update.
- Show the highest-ranked observation when no direction qualifies, but label it `观察/不下注`; never call it `主推`.

### Mandatory Codex archive

After every completed **pre-match** prediction, run:

```text
python <skill-dir>/scripts/memory_store.py record --analysis-stage initial [fields...]
```

The script stores records under `<workspace>/.codex/soccer-predict/history.json`. Pass `--base-dir <workspace>` when the current directory is not the intended workspace. Do not use `.openclaw` or `.claude` paths.

Include the match ID, league, kickoff, teams, 1X2 probabilities, source URL, concise rationale, `--data-quality`, and exactly two `--exact-score-pick SCORE:PROBABILITY` values. Keep `--predicted-score` equal to the first-ranked exact score. Always pass `--zero-zero-probability` and `--zero-zero-rank` from the same full distribution; add `--zero-zero-odds` and `--zero-zero-ev` only when the current 0-0 market price was collected. Archive only threshold-qualified recommendations in the dedicated Asian, total, goal-range, BTTS, corner-total, corner-handicap, first-half, and HT/FT pick fields; keep unqualified observations in notes so they do not pollute accuracy or ROI. Follow [references/expanded-markets.md](references/expanded-markets.md) for the new pick fields, evidence flags, odds formats, and settlement inputs. An edge value of `4` means four percentage points, not `0.04`. Pass the applicable machine-readable evidence flags: `--lineup-confirmed`, `--fundamental-evidence`, `--chance-quality-evidence`, `--attack-configuration-evidence`, `--corner-profile-evidence`, `--opponent-tail-risk-checked`, and `--injury-evidence-status`. For a deep favorite also pass `--asian-cover-probability` and `--asian-cover-distribution-validated`. Archive HT/FT recommendations with `--htft-pick SELECTION:ODDS:PROBABILITY:EV`. If guardrail validation or archiving fails, downgrade the candidate to observation or no primary and report the failure; never bypass the validator.

Pass `--primary-market` on every `record` call. Select exactly one archived formal direction as the machine-readable primary; the script marks every other formal direction `secondary`. Use `--primary-market none` only when no formal direction qualifies. A lineup check must explicitly persist `primary_change.status` as `maintained` or `changed`.

For a changed lineup-check primary, pass `--primary-change-reason` and recalculate both old and new directions at the current market. Every replacement requires high data quality, confirmed lineups, and a new safe Rank 1. If the old thesis remains eligible, pass `--previous-primary-current-confidence`; the new score must be at least five points higher. If confirmed hard information makes the old thesis ineligible, pass `--previous-primary-invalidated` and record the evidence instead of forcing a score margin. A worse same-direction line additionally requires `--accept-worse-line`. If the old thesis is invalid but no safe candidate qualifies, use `--primary-market none`; cancellation is valid.

The archive command is idempotent for identical predictions. If it returns `duplicate_ignored: true`, do not claim a new revision was created.

After every successful initial archive, run `wechat_formatter.py --base-dir <workspace> --match-id <id> --kind initial`. Append its exact plain-text output under `微信可复制版`, after the visual analysis. This copy block is mandatory even when automatic WeChat delivery is disabled; do not replace it with Markdown tables, HTML, or the visualization.

For every `微信可复制版` block, show the formatter output as ordinary wrapped text, not a fenced code block. Keep the full recommendation, notes, risks, and learning text except for an internally retained non-Top-2 0-0 diagnostic; never present an ellipsis-truncated field as a complete message.

## Automatic lineup-time reanalysis in Codex

Read [references/lineup-scheduling.md](references/lineup-scheduling.md) and [references/watchdog-runtime.md](references/watchdog-runtime.md) after every archived initial prediction. Register the verified kickoff with `lineup_scheduler.py`, explicitly using `Asia/Tokyo` for this user's local timezone and `Asia/Shanghai` for Titan Chinese pages. Exact per-match, one-time Codex automations are the default executor: create one T-30 task and one kickoff-plus-three-hour review task, each with `COUNT=1`. Do not install, enable, or run a global polling watchdog, minutely automation, background Python process, or PowerShell loop unless the user explicitly opts in to recurring polling. Never construct schedules from an unconverted wall-clock value, analyze earlier than T-30, or run after kickoff.

After the initial archive, register both the T-30 check and the kickoff-plus-three-hour review, create their two exact one-time automations, and attach the returned automation IDs to scheduler state. The internal retry plan is recovery metadata only; never materialize `retry-T-*` automations. At the beginning of every user-initiated soccer-predict invocation, run both schedulers' `sync-pending` commands, then `due`. If due work exists, create one new saved Codex task per match and let that task claim and run it. Never publish a lineup or review result in the originating initial-analysis task.

Every child task must resolve its exact saved task ID from `nodeRepl.requestMeta.threadId` and then claim the match before collecting data. Refuse to proceed when the ID is missing; never invent an ID or reuse the originating task ID. A claim lease prevents duplicate revisions; a failed attempt must release the lease, and an expired lease may be reclaimed. Do not call `complete` until `record --analysis-stage lineup-check` succeeds and a complete non-empty result artifact has been saved. Call `complete --thread-id <current_thread_id> --result-artifact <path>`, then immediately send the user-facing final answer in that same task.

Delivery precedes cleanup. The result task must not call `mark-delivered`, delete automations, call `mark-cleaned`, or archive itself. A later user-initiated soccer-predict invocation or an explicit one-time cleanup task must verify that the exact task has status `completed` and a non-empty final answer; only then may it call `mark-delivered`, remove exact automation references, and call `mark-cleaned`. Do not keep a polling process alive merely for cleanup. This keeps the newly created result task visible and prevents cleanup from racing ahead of delivery.

If archival succeeded but the worker stopped before saving its task ID/artifact, `cleanup-due` returns `await_complete_metadata`. The next user-initiated invocation or explicit one-time recovery task must create a new saved task that renders the archived result without duplicating settlement, persists its own task ID/artifact through `complete` or `terminal`, and sends the result before cleanup.

Delivery is mandatory for the one attempt that obtains the claim even when no odds, lineup, EV, or recommendation changed. Begin with `临场复查 <match_id>`, state the Japan-time check time and match status, and show `主推维持` or `主推变更`. Archive only no-op retry tasks that fail to obtain a claim.

After every successful lineup-check archive, run `wechat_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check` and append its exact output under `微信可复制版`. Generate it whether the primary changed or stayed the same.

If `<workspace>/.codex/soccer-predict/wechat_push.json` exists and has `enabled: true`, read [references/wechat-delivery.md](references/wechat-delivery.md). Require a successful same-session `--verify-draft-only` readiness check before unattended delivery; current WeChat 4.1+ may hide UIA per account, and Windows Narrator is not a recovery method. After a valid initial prediction is archived, send one separately formatted plain-text initial summary through the configured verified backend with event key `initial:<match_id>`. After the revised T-30 analysis and archive succeed, send one separately formatted plain-text lineup summary with event key `lineup-check:<match_id>`. Never paste the Codex Markdown/HTML visualization into WeChat. Treat WeChat as a secondary delivery channel: complete the Codex task regardless, report `微信已推送` only after `sent: true`, and on any target-verification, accessibility, readiness, or send failure report `微信未推送` without retrying, restarting WeChat/Narrator, or choosing another recipient. Never push post-match reviews through this setting.

Thread separation is mandatory:

- Keep the originating analysis task for the initial prediction only. Do not post the lineup-time result back into that task.
- The one-time executor must use the thread-creation tool and receive a real thread ID before acknowledging the scheduled item. Run the claimed attempt as that new saved project task and keep it visible in the task list. Use the title `临场复查 <match_id>｜<home_team> vs <away_team>`.
- When an immediate lineup-time check is required instead of a scheduled automation, locate the Codex thread-creation tool and create a new project task for the check. Return only the new-task confirmation in the originating task; put the reanalysis itself in the new task.
- If the thread-creation tool is unavailable, report that a separate lineup-check task could not be created. Do not silently fall back to publishing the reanalysis in the originating task.
- A user request made inside the originating task to perform the lineup-time check still creates a new task. Follow-up discussion may continue inside that new lineup-check task.

Do not silently count an initial prediction as the delivered lineup-time reanalysis. Apply these timing rules:

- If more than 30 minutes remain, return the initial prediction now and create the distinct one-time automation for T-30; do not run the check early, including after an explicit request.
- If T-30 is less than two minutes away, create a new Codex task and run the lineup-time reanalysis there immediately.
- If fewer than 30 minutes remain but the match is still prematch, create a new Codex task, run the lineup-time reanalysis there immediately, and label it late.
- If the user explicitly requests a lineup-time check before T-30, schedule it for T-30. A requested check at or after T-30 may run immediately and count as the one check, using the required `临场复查 <match_id>` output and delivery format.
- If kickoff time or timezone is uncertain, do not guess the schedule; report that automatic reanalysis could not be scheduled.

## Review

Read [references/review-framework.md](references/review-framework.md). A review may be triggered by:

- `复盘 <match_id>`
- `比分 <home>-<away>` when the match is unambiguous
- an automation prompt containing the match ID

Treat `status: reviewed` as a terminal state. Before fetching scores or generating a review, inspect the archived record. If the match is already reviewed, do not fetch data, run settlement again, rewrite the record, produce another full review, or schedule another review. Return only a brief notice that the review is already complete, together with the stored final score and review time when available.

For a standalone review task, normalize the league first and title it `复盘｜<league_key>｜<match_id>｜<home_team> vs <away_team>`. Begin the visual review with the same league label so review tasks remain easy to scan by competition; keep each match in its own task.

Apply a hard terminal-state gate before every manual or automatic review:

- Open the Titan live/detail page and require an explicit terminal status such as `完`, `完场`, or `Finished`. A visible score, a 90+ minute clock, half-time, extra time in progress, or penalties in progress is not proof that the match has ended.
- If the page says `未`, `进行中`, `中场`, `加时中`, `点球中`, `推迟`, `中断`, `取消`, or the status is missing/conflicting, do not review and do not call `memory_store.py review`.
- When the terminal state cannot be verified, return `比赛未确认完场，暂不复盘`, leave the archive `pending`, and preserve all prediction fields.
- A user-supplied score may bypass the page check only when the user explicitly states that it is the completed final score. For automatic review, page verification is always mandatory.
- For cup matches that finish after extra time or penalties, wait until the whole match is terminal, then settle football and corner markets using the verified 90-minute results unless the archived market explicitly includes extra time.

Fetch the verified final score from titan007 when the user does not supply it. Only after the terminal-state gate passes, run:

```text
python <skill-dir>/scripts/memory_store.py review --verified-finished --match-id ... --half-home-score ... --half-away-score ... --home-score ... --away-score ... [--home-corners ... --away-corners ...] --key-learning "..."
python <skill-dir>/scripts/memory_store.py stats
python <skill-dir>/scripts/memory_store.py calibrate --write
```

Settle the final active pre-match version only. When an archived `lineup-check` exists, its primary and formal picks replace the initial version for official win/loss, accuracy, profit, and ROI; use the initial revision only for diagnostic comparison. Fall back to the initial version only when no valid lineup-check was archived. Require the review result to persist `settlement_basis.policy: latest_active_prematch_version` and verify its `analysis_stage` before reporting the outcome.

Only the final active primary affects direction accuracy, profit, and ROI. A reviewed match whose final active version has no primary must not change matches, wins/losses/pushes, stake, profit, accuracy, or ROI; persist it instead as `learning_scope: no_primary_observation` with `counts_toward_primary_record: false`, a non-empty causal learning, and include it in league/global learning samples. Keep secondary picks as pre-match references only: never settle them, record their hit/miss outcome, include them in any performance denominator, assign them a stake, or calculate profit/loss and ROI. Explain the diagnostic Top-1/Top-2 exact-score result, the primary result or explicit no-primary status, cumulative primary statistics, current-league primary statistics, and the saved learning. If a half-time/HTFT primary lacks a verified half-time score, or a corner primary lacks both official 90-minute corner counts, leave the record pending rather than inventing or partially settling it.

After `review`, `stats`, and `calibrate --write` succeed, run `wechat_formatter.py --base-dir <workspace> --match-id <id> --kind review`. Append its exact output under `微信可复制版`. This review copy is for the user to send manually; never treat its generation as authorization to push a post-match review to WeChat.

When reporting `战绩`, `准确率`, or `ROI`, use `stats.primary`: one final active primary per match, using the lineup-check primary whenever present. State reviewed workflow count separately when useful; never describe no-primary learning samples as record matches. Use `stats.learning_samples.no_primary_observation` for their learning count and `stats.primary_by_market` only to break primary results down by market. Ignore legacy secondary result fields and never mix superseded initial picks into statistics. In a single-match review, label secondary picks `仅赛前参考，不结算` without showing win/loss/push. Exact-score accuracy is diagnostic only and never enters primary accuracy.

`--key-learning` is mandatory. For a primary review, identify the causal assumption behind the final active primary that was confirmed or rejected. For a no-primary review, explain why no candidate cleared the gate and what verified result evidence teaches about the abstention threshold, data quality, or model tail risk; do not grade the observation as a bet. Do not mention a secondary pick as hit/missed or use its outcome as calibration evidence. Do not use generic text such as “模型需优化”. The script preserves the raw competition label but groups learning by normalized `league_key`, so season/round variants of the same league share one profile. The calibration snapshot is durable workspace memory; it summarizes global and league-level primary accuracy, ROI, no-primary observation learning, market-signal splits, recent causal learnings, and whether the sample is large enough for weight changes.

Do not automatically change global or league-specific weights from a tiny sample. Require at least 20 graded selections in the affected market within the relevant scope plus feature-level evidence before saving a weight override. With fewer samples, keep weights unchanged and save only provisional guardrails or data-quality lessons.

## Automatic review in Codex

Read [references/review-framework.md](references/review-framework.md) and [references/watchdog-runtime.md](references/watchdog-runtime.md). Automatic review is part of every successfully archived pre-match prediction for this installation. Register the match idempotently with `review_scheduler.py`; the first status check is kickoff plus three hours in `Asia/Tokyo`. The time only makes the check due and is never proof of full-time.

The exact kickoff-plus-three-hour automation may inspect only the named match and scheduler item. When no event is due, it must not open Titan, calculate, settle, or emit a substantive result. For each due review event it must create a new saved task titled `复盘｜<league_key>｜<match_id>｜<home_team> vs <away_team>` and acknowledge the item only after a real task ID is returned.

The review task must claim before browsing. If Titan is explicitly final, run settlement exactly once, save the complete result artifact, call `review_scheduler.py complete --thread-id <current_thread_id> --result-artifact <path>`, and immediately send the final answer. Do not clean or archive the result task. A later user-initiated invocation or explicit one-time cleanup task verifies completed delivery and performs cleanup.

If the status is non-terminal, call `review_scheduler.py wait` and archive that no-result check. This creates exactly one non-duplicate follow-up for 30 minutes later; the next due event must open a fresh review task. For postponed, cancelled, or abandoned matches, save a complete terminal notice and use `terminal`; do not settle the prediction.

The one-time Codex automation can run only when its local execution environment is available. It cannot run while the computer is powered off or the user is logged out. Never claim otherwise.

## Local data commands

```text
python <skill-dir>/scripts/memory_store.py pending
python <skill-dir>/scripts/memory_store.py stats
```

Use these before answering questions about pending reviews or historical accuracy.
