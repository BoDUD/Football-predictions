---
name: soccer-predict
description: >
  Analyze football matches from titan007.com with a reproducible canonical score model,
  strict pre-kickoff evidence, Asian handicap, totals, goal ranges,
  both-teams-to-score, corner totals and handicaps, European odds, exact scores, first-half
  and half-time/full-time markets, fundamentals, lineups, and post-match review. Use for match IDs or descriptions,
  football predictions, multi-market value analysis, result reviews, accuracy statistics,
  copyable plain-text summaries, automatic checks around 30 minutes before kickoff,
  and automatic Codex review scheduling after a match.
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

Read [references/model-validation.md](references/model-validation.md), [references/prediction-framework.md](references/prediction-framework.md), [references/expanded-markets.md](references/expanded-markets.md), [references/exact-score.md](references/exact-score.md), [references/half-time-full-time.md](references/half-time-full-time.md), and [references/image-output.md](references/image-output.md), then:

- Before calculating a new prediction, run `memory_store.py stats` and read `<workspace>/.codex/soccer-predict/calibration.json` when it exists. Lead with its strict out-of-sample cohort and current `market_status`; never blend legacy, backfill, invalid-timing, or force/rewrite records into a current-performance claim.
- Produce one prediction artifact with `scripts/score_model.py predict --kickoff <timezone-aware-ISO>` from a versioned fitted model. It must prove that the training cutoff predates the fixture and generation predates kickoff. All football 1X2, exact-score, totals, Asian handicap, goal-range, and BTTS probabilities must reproduce sums from that artifact's single score matrix. For a supported league, use `scripts/league_model_manager.py predict` so the registered `scripts/htft_model.py` artifact and its embedded canonical full-score component are selected together. Require the registry hash, competition key, dataset-manifest hash, cutoff, artifact hashes, and full-time marginal consistency check to pass; a mismatch disables the HT/FT output rather than creating a second full-time opinion. The registered manager permits a verified half-time marginal anchor but rejects a full-time external anchor until it can update the canonical score matrix too. If no suitable model exists, or an artifact's matrix/tail/provenance validation fails, show model-based directions only as observations and archive no formal primary.
- Default to image-plus-text `可视化模式` for every initial or lineup-time prediction unless the user explicitly asks for text-only, `简洁模式`, `简洁`, `concise`, or `short`. Build the deterministic poster with `scripts/prediction_card_renderer.py`, bind it to the workspace `history.json`, display it from an absolute local path, then provide normal analytical text that adds probability, EV/evidence, failed-gate, and risk context without repeating a second `可复制纯文本版` summary. Reviews preserve the canonical normal review text; do not force a prediction-table poster onto a settled result.
- The poster must show recommendation status, competition/teams, strongest direction, goal range, two HT/FT probability shapes, and two score references. Supply HT/FT and scores as two-element arrays. The renderer derives `★` only from a unique active archived formal primary that passed every gate; callers never supply the star. Use `◇` for the strongest observation and `无正式推荐` for no-bet rows; never let an observation, model probability, user preference, or historical hit rate receive a star. A daily slate may contain multiple starred matches but at most one starred direction per match.
- Accompanying visual-mode text must show opening-to-current market movement, no-vig probabilities, EV comparison, key fundamentals/lineups, recommendations, predicted score, and risks. Put Asian handicap, totals, goal ranges, BTTS, corner totals, and corner handicaps into one comparable candidate table when that detail materially helps; do not transcribe every poster cell.
- Visual mode must also show **exactly two ranked exact-score candidates**, a first-half panel, and a 3x3 HT/FT probability matrix. Show at most one first-half pick and **exactly two probability-selected HT/FT scenarios** whenever the model matrix is available.
- For every valid pre-match or lineup-check prediction, preserve the unconditional exact-score Top 2 as the distribution audit. When the unique formal primary is a full-time total, show a separate pair ranked inside that primary's net-profit branch so the user-facing scenarios support the main thesis. Show each scenario's unconditional probability and its conditional share; label both `主推成立时的波胆情境（高方差参考，不计主推）`. Without a formal total primary, display the unconditional Top 2.
- Always evaluate and archive 0-0 in the same full score distribution before display conditioning. In an unconditional display, let it appear naturally when it ranks first or second. In a total-primary display, show it only when it belongs to the primary's net-profit branch and ranks in that displayed pair; otherwise keep the audit internal unless the user explicitly requests the diagnostic. Odds such as 7.00 never trigger display by themselves. Follow `references/exact-score.md`; the standalone exact-score ranker is legacy diagnostics only and must not create a second strict-forward distribution.
- When a non-Top-2 0-0 audit is hidden, do not repeat its probability, rank, odds, or EV inside recommendation, notes, risks, visual captions, or copyable plain-text output. Keep those values only in the machine-readable archive.
- First compute each HT/FT scenario's diagnostic qualification status, then apply the active market policy; `strict-oos-market-policy-v1` always downgrades HT/FT to `观察候选（未达标）`, and the ranker must return `formal_count: 0`. Validate that the 3x3 matrix reproduces the displayed half-time row marginals and full-time column marginals, then use `scripts/htft_ranker.py` with `probability_top2_v3_post_selection`. Select the two largest joint probabilities with the canonical HH, HD, HA, DH, DD, DA, AH, AD, AA tie-break; conditional follow-through, state continuity, full-time coherence, exact-score result classes, odds, and EV are audits and qualification gates only and must not replace either slot. Pass the normalized `league_key`, current model hash, and the registry-issued `league_pair_gate_evidence`; never use a source-code table of historical league results. Treat the evidence threshold, covered/hit counts, deployment status, regime warning, and eligibility flags as valid only when their dataset, evaluation, model, and league hashes all bind to the current artifacts. Missing, stale, mismatched, or unsupported evidence cannot receive a confidence label. Partial-2026 cohorts are shadow-only, and only future clean live-forward samples can confirm the end-to-end selector. A verified half-time market anchor has no promoted pair-mass gate and must be labelled `anchor_gate_unvalidated`; metrics from an untimestamped full-time-opening research cohort must never be applied to a live half-time anchor. Preserve both display slots as observations. Do not retune thresholds from a reviewed live pick.
- For both HT/FT scenarios show slot (`主概率形态` / `备选概率形态`), selection, current odds, model probability, combined pair probability, conditional follow-through, state continuity, no-vig market probability, model-versus-market edge, EV, confidence-gate result, and status. Do not show a rank column. When current odds are unavailable, keep the same two probability-selected scenarios and label them `赔率缺失，不可执行`; never invent odds or EV.
- An HT/FT scenario can be diagnostically qualified only when all nine current executable odds are present and the ranker derives the no-vig distribution from those prices. Caller-supplied market probabilities with partial odds are audit annotations only and must add `complete current 9-way HT/FT odds unavailable`; they can never qualify a scenario or bypass the active observation-only policy.
- The complete nine prices must describe the same current executable HT/FT market and carry source plus pre-kickoff collection time. `league_key` is also mandatory for league evidence. Without either the complete current 9-way market or league context, keep both scenarios non-executable observations.
- Preserve every imported Titan `format_version`, `phase_group`, `season_status`, and `competition_regime` value for audit and evaluation slices. This does not mean the current manager fits an independent model for every phase: registered training and fixed-season component scoring accept only `competition_regime=regular`; special regimes retain exclusion counts and drift warnings. Treat every `season_status=partial_as_of_*` test cohort, including an unfinished 2026 season, as research/shadow and exclude it from promotion evidence.
- The expanded bundle targets 14 competition-specific histories: Brazil Serie A, Japan J1, Norway Eliteserien, MLS, the five major European leagues, Korean K League 1, Allsvenskan, Finland Veikkausliiga, UEFA Champions League, and AFC Champions League. Read the exact row counts from the current dataset manifest and each league's `deployment_status` and pair-gate evidence from the hash-bound registry; never copy an old count or hard-code which leagues are `candidate` or `shadow`. A `candidate` requires at least 100 known-team fixtures in the fixed-2025 holdout and negative means plus negative paired-bootstrap 95% CI upper bounds for both log-loss and Brier deltas versus the training-window empirical baseline; otherwise it is `shadow`. Historical classification evidence is not live betting proof. All entries remain `formal_htft_eligible=false`, and an incomplete `season_status=partial_as_of_*` cohort is research/shadow only.
- Corner history and walk-forward backtests are also model-development evidence only. Require a prediction from `corner_model_manager.py`, verify `formal_corner_total_eligible` and `formal_corner_handicap_eligible`, and currently keep both markets as `◇` observations because both flags are false. Do not use historical fit, a caller assertion, or current-market EV to manufacture a formal corner pick; a future manager must bind separate strict live-forward validation before either flag may be enabled.
- Concise mode: return only the best direction, probability, EV, exactly two ranked exact scores, and one short rationale. Do not add a separate 0-0 audit unless explicitly requested.
- If some visual fields are unavailable, keep the section visible and mark them `数据未取得` or `待公布`; never invent values to fill the layout.
- Use current pre-match odds for final calculations and opening odds only for movement analysis.
- Handle quarter lines with their real half-win/half-loss settlement; do not reduce them to a binary outcome.
- State when lineups are unconfirmed or data is incomplete.

### Provisional recommendation gate

- Classify every evaluated market direction as `正式推荐` or `观察候选（未达标）`.
- Under `strict-oos-market-policy-v1`, Asian handicap, first-half, HT/FT, corner totals, and corner handicaps are observation-only. Do not archive them as a formal primary until a later versioned policy explicitly re-enables the market from clean forward calibration evidence. Legacy/backfilled results and historical corner backtests do not unlock them.
- A formal direction needs positive current EV, a positive comparable model-versus-no-vig edge, medium or high data quality, a complete executable current market, and every market-specific evidence check below. Missing or non-positive inputs make it an observation.
- If the current consensus line and related 1X2/total market materially oppose the selection (`against` or materially `conflicting`), require EV >= 8%, edge >= 4 percentage points, at least five firms, and independent confirmed-lineup or fundamental evidence; otherwise keep it as an observation.
- For any formal total-goals direction, additionally require at least five firms plus chance-quality evidence or a confirmed attacking configuration. A price drop, historical over rate, or H2H score pattern cannot satisfy this evidence gate alone.
- For a goal-range or BTTS formal direction, require the complete mutually exclusive market, real current odds, and either chance-quality evidence or an attacking configuration supported by confirmed lineups. Derive its model probability from the same full score matrix used for exact scores; never substitute a totals or exact-score price.
- For a future policy-enabled corner-total or corner-handicap formal direction, first require the matching corner manager formal flag, then a complete two-way current market, at least three firms, independent corner-profile evidence, and a full-win/half-win/push/half-loss/loss probability distribution whose nonzero states are possible at that line. The current manager flags are false, so these outputs stay observations. Build a separate corner distribution from home/away corners for and against, width/crossing, dangerous attacks, set pieces, and confirmed personnel; never use football xG as a corner probability proxy.
- Rank only safe formal candidates with the versioned policy returned by `memory_store.py`. Count model probability through EV once; do not present settlement probability, EV, and edge as three independent confirmations. Use genuinely separate robustness inputs such as settlement variance, market depth, data freshness, source agreement, and strict-OOS calibration. Select exactly one primary only when its lead survives sensitivity checks.
- For every formal direction, including Asian, totals, first-half, and HT/FT, archive odds format, the complete current market, no-vig market probability, source, timezone-aware collection time, consensus/median price basis, bookmaker count, and all required settlement states. Let the archive command recalculate edge and EV; supplied values are assertions only. If any audit disagrees or misses the gate, downgrade it to observation.
- For a selected favorite at `-0.75` or deeper, require high data quality, confirmed lineups, an opponent counterattack/goalkeeper/set-piece tail-risk check, and an independently calculated goal-margin/cover distribution. Support the cover thesis with independent chance-quality evidence; alternatively accept an aligned market from at least five firms only when confirmed attacking configuration and fundamental evidence also agree. Never substitute 1X2 win probability, team reputation, possession, or a strong XI for cover probability.
- Keep these safety and evidence gates active until a feature-level review explicitly changes them. Reaching 20 graded selections does not relax them automatically or authorize a weight update.
- Show the highest-ranked observation when no direction qualifies, but label it `观察/不下注`; never call it `主推`.

### Mandatory Codex archive

After every completed **pre-match** prediction, run:

```text
python <skill-dir>/scripts/memory_store.py record --analysis-stage initial [fields...]
```

The script stores records under `<workspace>/.codex/soccer-predict/history.json`. Pass `--base-dir <workspace>` when the current directory is not the intended workspace. Do not use `.openclaw` or `.claude` paths.

Include the match ID, league, teams, source URL, concise rationale, `--data-quality`, and the full time-state tuple: `--page-status prematch`, `--source-kickoff`, `--source-timezone`, `--user-local-kickoff`, `--user-timezone`, plus the timezone-aware `--kickoff`. Pass `--model-version` and `--score-model-file prediction.json`; the archive hashes and snapshots the canonical matrix artifact. Every formal football-goal pick requires this artifact. It may be omitted only when there is no formal football-goal direction, such as a no-primary or independently modeled corner-only record.

Pass 1X2 probabilities and exactly two unconditional `--exact-score-pick SCORE:PROBABILITY` values that reproduce the score matrix. Keep `--predicted-score` equal to unconditional rank 1. For a formal total primary, also pass exactly two `--display-exact-score-pick SCORE:PROBABILITY:UNCONDITIONAL_RANK` values and `--display-exact-score-event-probability`. Always pass `--zero-zero-probability` and `--zero-zero-rank` from the same artifact; add 0-0 odds/EV only when its current market was collected.

For any formal Asian, total, or first-half line, pass its odds format, complete-market flag, source, collection time, consensus/median price basis, bookmaker count, and all five `full-win`, `half-win`, `push`, `half-loss`, and `loss` probabilities. Supply every current outcome price with repeated `--<prefix>-market-odds LABEL:PRICE`; the archive command calculates no-vig probabilities itself and treats any supplied market probability as an assertion. HT/FT needs all nine prices; goal-range bands must be continuous from zero through an open-ended final band. Follow [references/expanded-markets.md](references/expanded-markets.md) for exact labels. An edge value of `4` means four percentage points, not `0.04`; EV and edge are recomputed server-side.

Archive only threshold-qualified and policy-enabled recommendations in dedicated pick fields. Keep unqualified or paused-market observations out of all formal pick fields. When a valid registered HT/FT prediction and its matching ranker output exist, pass both `--htft-observation-model-file` and `--htft-observation-ranker-file`; the archive verifies their hashes, fixture, cutoff, matrix, Top 2, pair mass, and per-gate failures, then stores a `candidate_audits` observation that never counts as a primary or monetary result. Pass applicable evidence flags: `--lineup-confirmed`, `--fundamental-evidence`, `--chance-quality-evidence`, `--attack-configuration-evidence`, `--corner-profile-evidence`, `--opponent-tail-risk-checked`, and `--injury-evidence-status`. If validation or archiving fails, use no primary and report the failed audit; never use a force overwrite or a backfill to repair a prediction after kickoff.

Pass `--primary-market` on every `record` call. Select exactly one archived formal direction as the machine-readable primary; the script marks every other formal direction `secondary`. Use `--primary-market none` only when no formal direction qualifies. A lineup check must explicitly persist `primary_change.status` as `maintained` or `changed`.

For a changed lineup-check primary, pass `--primary-change-reason` and recalculate both old and new directions at the current market. Every replacement requires high data quality, confirmed lineups, and a new safe Rank 1. If the old thesis remains eligible, pass `--previous-primary-current-confidence`; the new score must be at least five points higher. If confirmed hard information makes the old thesis ineligible, pass `--previous-primary-invalidated` and record the evidence instead of forcing a score margin. A worse same-direction line additionally requires `--accept-worse-line`. If the old thesis is invalid but no safe candidate qualifies, use `--primary-market none`; cancellation is valid.

The archive command is idempotent for identical predictions and append-only for accepted
revisions. If it returns `duplicate_ignored: true`, do not claim a new revision was created.
It must reject initial records at or after kickoff, lineup checks outside the verified
pre-kickoff window, and attempts to overwrite a reviewed or different existing record.
Backfill/migration data remain visibly quarantined from strict forward statistics.

After every successful initial archive, run `plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind initial` to verify the canonical text contract. In the default image-plus-text response, preserve its primary or explicit no-primary status, score references, rationale, risks, and cumulative record once in the normal accompanying text; do not append a duplicate `可复制纯文本版` block. If the user explicitly requests a copyable block or text-only mode, append the formatter's exact output under that heading. Follow [references/plain-text-output.md](references/plain-text-output.md) for the three output contracts.

Whenever a `可复制纯文本版` block is explicitly requested, show the formatter output as ordinary wrapped text, not a fenced code block. Keep the full recommendation, notes, risks, and learning text except for an internally retained non-Top-2 0-0 diagnostic; never present an ellipsis-truncated field as a complete message.

## Automatic lineup-time reanalysis in Codex

Read [references/lineup-scheduling.md](references/lineup-scheduling.md) and [references/watchdog-runtime.md](references/watchdog-runtime.md) after every archived initial prediction. Register the verified kickoff with `lineup_scheduler.py`, explicitly using `Asia/Tokyo` for this user's local timezone and `Asia/Shanghai` for Titan Chinese pages. Exact per-match, one-time Codex automations are the default executor: create one T-30 task and one kickoff-plus-three-hour review task, each with `COUNT=1`. Do not install, enable, or run a global polling watchdog, minutely automation, background Python process, or PowerShell loop unless the user explicitly opts in to recurring polling. Never construct schedules from an unconverted wall-clock value, analyze earlier than T-30, or run after kickoff.

After the initial archive, register both the T-30 check and the kickoff-plus-three-hour review, create their two exact one-time automations, and attach the returned automation IDs to scheduler state. The internal retry plan is recovery metadata only; never materialize `retry-T-*` automations. At the beginning of every user-initiated soccer-predict invocation, run both schedulers' `sync-pending` commands, then `due`. If due work exists, create one new saved Codex task per match and let that task claim and run it. Never publish a lineup or review result in the originating initial-analysis task.

Every child task must resolve its exact saved task ID from `nodeRepl.requestMeta.threadId` and then claim the match before collecting data. Refuse to proceed when the ID is missing; never invent an ID or reuse the originating task ID. A claim lease prevents duplicate revisions; a failed attempt must release the lease, and an expired lease may be reclaimed. Do not call `complete` until `record --analysis-stage lineup-check` succeeds and a complete non-empty result artifact has been saved. Call `complete --thread-id <current_thread_id> --result-artifact <path>`, then immediately send the user-facing final answer in that same task.

Delivery precedes cleanup. The result task must not call `mark-delivered`, delete automations, call `mark-cleaned`, or archive itself. A later user-initiated soccer-predict invocation or an explicit one-time cleanup task must verify that the exact task has status `completed` and a non-empty final answer; only then may it call `mark-delivered`, remove exact automation references, and call `mark-cleaned`. Do not keep a polling process alive merely for cleanup. This keeps the newly created result task visible and prevents cleanup from racing ahead of delivery.

If archival succeeded but the worker stopped before saving its task ID/artifact, `cleanup-due` returns `await_complete_metadata`. The next user-initiated invocation or explicit one-time recovery task must create a new saved task that renders the archived result without duplicating settlement, persists its own task ID/artifact through `complete` or `terminal`, and sends the result before cleanup.

Delivery is mandatory for the one attempt that obtains the claim even when no odds, lineup, EV, or recommendation changed. Begin with `临场复查 <match_id>`, state the Japan-time check time and match status, and show `主推维持` or `主推变更`. Archive only no-op retry tasks that fail to obtain a claim.

After every successful lineup-check archive, run `plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check` and preserve its required information once in the normal image-plus-text result, whether the primary changed, stayed the same, or was cancelled. Append the exact raw block only on an explicit copyable/text-only request. This skill has no external-message sender and must not attempt to deliver the text to any chat application.

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
python <skill-dir>/scripts/memory_store.py review --verified-finished --match-id ... --half-home-score ... --half-away-score ... --home-score ... --away-score ... [--home-corners ... --away-corners ...] --verification-source ... --verification-collected-at ... --key-learning "..."
python <skill-dir>/scripts/memory_store.py stats
python <skill-dir>/scripts/memory_store.py calibrate --write
```

Settle the final active pre-match version only. When an archived `lineup-check` exists, its primary and formal picks replace the initial version for official win/loss, accuracy, profit, and ROI; use the initial revision only for diagnostic comparison. Fall back to the initial version only when no valid lineup-check was archived. Require the review result to persist `settlement_basis.policy: latest_active_prematch_version` and verify its `analysis_stage` before reporting the outcome.

Only the final active primary affects direction accuracy, profit, and ROI. A reviewed match whose final active version has no primary must not change matches, wins/losses/pushes, stake, profit, accuracy, or ROI; persist it instead as `learning_scope: no_primary_observation` with `counts_toward_primary_record: false`, a non-empty causal learning, and include it in league/global learning samples. A frozen structured HT/FT observation may be graded diagnostically against the verified half-time/full-time result for Top-1, Top-2, nine-class Brier, log loss, and gate-funnel learning, but it remains `counts_toward_primary_record: false` with no stake, win/loss, profit, or ROI. Keep secondary picks as pre-match references only: never settle them, record their hit/miss outcome, include them in any performance denominator, assign them a stake, or calculate profit/loss and ROI. Explain the diagnostic Top-1/Top-2 exact-score result, the primary result or explicit no-primary status, cumulative primary statistics, current-league primary statistics, and the saved learning. If a formal half-time/HTFT primary lacks a verified half-time score, or a corner primary lacks both official 90-minute corner counts, leave the record pending rather than inventing or partially settling it; a missing half-time score merely leaves an observation ungraded and must not block an otherwise valid no-primary/full-time review.

After `review`, `stats`, and `calibrate --write` succeed, run `plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review`. Preserve its required information once in the normal image-plus-text result; append the exact raw block only on an explicit copyable/text-only request. Always keep lines such as `主推：无正式推荐（不结算、不计战绩）`, score references, the causal learning, league record, and cumulative record even when no formal primary existed. The formatter only renders text inside Codex; it never sends messages externally.

When reporting current `战绩`, `准确率`, or `ROI`, lead with the strict out-of-sample primary cohort and state its sample size. Report legacy, backfill, invalid-timing, and force/rewrite cohorts separately; never blend them into the headline. Use the per-market breakdown, calibration metrics, coverage/abstention, and model version where available. A lineup-check still supersedes the initial version inside the same clean match. Exact-score accuracy remains diagnostic only.

`--key-learning` is mandatory. For a primary review, identify the causal assumption behind the final active primary that was confirmed or rejected. For a no-primary review, explain why no candidate cleared the gate and what verified result evidence teaches about the abstention threshold, data quality, or model tail risk; do not grade the observation as a bet. Do not mention a secondary pick as hit/missed or use its outcome as calibration evidence. Do not use generic text such as “模型需优化”. The script preserves the raw competition label but groups learning by normalized `league_key`, so season/round variants of the same league share one profile. The calibration snapshot is durable workspace memory; it summarizes global and league-level strict-OOS performance, coverage/abstention, probability scoring, quarantined cohorts, market-signal splits, recent causal learnings, and whether a market has reached the minimum manual-review sample.

Do not automatically change model parameters or market policy from a small selected-bet sample. Require clean walk-forward evidence and proper scoring rules as described in `references/model-validation.md`; at least 20 graded strict-OOS selections is only a minimum review trigger, not proof of calibration. `calibrate --write` summarizes evidence and guardrails—it does not fit `score_model.py` or authorize a market automatically.

## Automatic review in Codex

Read [references/review-framework.md](references/review-framework.md) and [references/watchdog-runtime.md](references/watchdog-runtime.md). Automatic review is part of every successfully archived pre-match prediction for this installation. Register the match idempotently with `review_scheduler.py`; the first status check is kickoff plus three hours in `Asia/Tokyo`. The time only makes the check due and is never proof of full-time.

The exact kickoff-plus-three-hour automation may inspect only the named match and scheduler item. When no event is due, it must not open Titan, calculate, settle, or emit a substantive result. For each due review event it must create a new saved task titled `复盘｜<league_key>｜<match_id>｜<home_team> vs <away_team>` and acknowledge the item only after a real task ID is returned.

The review task must claim before browsing. If Titan is explicitly final, run settlement exactly once, save the complete result artifact, call `review_scheduler.py complete --thread-id <current_thread_id> --result-artifact <path>`, and immediately send the final answer. Do not clean or archive the result task. A later user-initiated invocation or explicit one-time cleanup task verifies completed delivery and performs cleanup.

If the status is non-terminal, call `review_scheduler.py wait --thread-id <current_thread_id>` and archive that no-result check. This creates exactly one non-duplicate follow-up for 30 minutes later; the next due event must open a fresh review task. For postponed, cancelled, or abandoned matches, save a complete terminal notice and use `terminal --thread-id <current_thread_id>`; do not settle the prediction.

The one-time Codex automation can run only when its local execution environment is available. It cannot run while the computer is powered off or the user is logged out. Never claim otherwise.

## Local data commands

```text
python <skill-dir>/scripts/memory_store.py pending
python <skill-dir>/scripts/memory_store.py stats
```

Use these before answering questions about pending reviews or historical accuracy.
