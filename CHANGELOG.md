# Changelog

## [3.2.0] - 2026-08-03

### Added
- Expanded the audited 2020–2026 history contract to fourteen competitions, including Finland Veikkausliiga, with 2026 retained as a partial-at-cutoff research cohort.
- Added source-bound regulation-time corner-result and pre-kickoff corner-price collectors, offline schedule normalization, league dataset generation, chronological corner models, registered replay validation, and fixed untouched holdouts.
- Added a deterministic daily prediction-card renderer that derives `★`, `◇`, and no-primary status from the archived active record instead of caller-supplied labels.

### Changed
- Hardened HT/FT evaluation and registration around fixed known-team holdouts, paired baseline uncertainty, dataset/model/evaluation lineage, and fail-closed non-formal deployment flags.
- Kept corner totals, corner handicaps, and HT/FT observation-only until separate clean live-forward evidence enables a later policy; historical `candidate` status never authorizes a formal pick.
- Preserved legacy and no-primary review wording and learning records while keeping them outside primary win rate, stake, profit, and ROI.

## [3.1.0] - 2026-08-02

### Added
- Added a strict XLSX importer for 9,211 finished Brazil Serie A, Japan J1, Norway Eliteserien, and MLS matches, including season rollover handling, timezone conversion, data hashes, and explicit quarantine of closing odds and ranks.
- Added deterministic per-league HT/FT artifacts built from separate half-time/full-time Dixon-Coles marginals, historical nine-cell association, and iterative proportional fitting.
- Added fixed-season HT/FT holdout evaluation, registered league-model management, paired canonical full-time score artifacts, and reproducible validation evidence.

### Changed
- Replaced `scenario_stability_v2` with `probability_top2_v3_post_selection`: the two displayed HT/FT shapes are the largest joint probabilities, while stability, coherence, exact-score agreement, odds, and EV remain audits only. Because 2025/2026 evidence was inspected during this selector's development, it is labelled post-selection component evidence rather than untouched end-to-end confirmation.
- Added league-scoped reporting for the descriptive 0.46 model-only pair-mass threshold while retaining HT/FT as observation-only; no supported league's 2025 Wilson 95% lower bound clears 50%. The 0.50 gate is explicitly confined to the untimestamped full-time-opening research cohort and cannot be claimed by a live half-time anchor.
- Classified all 180 Japan J1 2026 regional-format fixtures as `2026_vision_regional`, excluded special regimes from regular-only production training and formal evaluation metrics, and retained only exclusion counts for competition-regime drift auditing.
- Preserved legacy and no-primary review records as learning data without blending them into strict out-of-sample win rate or ROI.

## [3.0.0] - 2026-08-02

### Added
- Added a reproducible, time-decayed Poisson/Dixon-Coles baseline with deterministic model artifacts and one canonical score matrix for all football-goal markets.
- Added an expanding-window, date-grouped walk-forward evaluator with 1X2 log loss/Brier, exact-score log loss, and ordered goal-band RPS.
- Added strict pre-kickoff/model/market provenance, complete five-state settlement audits, server-side EV/edge checks, result-source evidence, and cross-process ledger locking.
- Added canonical validation for both unconditional exact-score Top 2 and total-primary branch display Top 2, including event mass, cell probability, and unconditional rank.
- Added strict-OOS performance cohorts, selection coverage/abstention, canonical-model 1X2 proper scores, per-market calibration/ROI reporting, timing/backfill/rewrite quarantine, and CI.

### Changed
- Replaced prose-only Logistic Regression weights, unsupported bookmaker-intent narratives, and fixed injury goal increments with an executable model and explicit sensitivity analysis.
- Made the prediction ledger append-only for accepted revisions and rejected post-kickoff or ambiguous-time archives.
- Paused Asian handicap, first-half, and HT/FT as strict-forward formal markets until clean forward calibration evidence supports a later policy version.
- Required scheduler task ownership and complete result artifacts before lineup/review completion.
- Removed the automatic WeChat sender, RPA fallback, delivery configuration, and delivery tests while retaining the same copyable primary/no-primary plain-text summaries under a channel-neutral formatter.

## [2.2.1] - 2026-07-29

### Changed
- Preserved the unconditional exact-score Top 2 for model audit while displaying two primary-consistent score scenarios when the formal main pick is a full-time total.
- Added explicit full-match and primary-conditioned probabilities for user-facing score scenarios, with integer-line pushes excluded from the supporting branch.
- Replaced mechanical HT/FT probability ranking with two `scenario_stability_v1` selections based on conditional follow-through, joint support, full-time support, and state continuity.
- Retained the legacy HT/FT `ranking_basis` and `top_two` JSON fields as compatibility aliases.

## [2.2.0] - 2026-07-28

### Changed
- Added the forward-only `stability-v1` primary policy: ordinary directions no longer require EV ≥ 8%, but every safe candidate must have positive EV and edge, medium/high data quality, a complete executable market, and market-specific evidence. The unique highest-confidence safe candidate becomes primary; no-bet remains valid when none is safe.
- Retained the stricter EV ≥ 8%, edge ≥ 4pp, five-firm, and independent-evidence gate when the handicap and related European market move materially against a candidate.
- Kept historical records immutable under `stability-v1`: prior observations are not promoted and old performance is not recalculated. Match 2912847 is documented only as a forward-rule audit example, not a backfill.
- Expanded the formal candidate pool to goal ranges, both-teams-to-score, corner totals, and corner handicaps without giving handicap or totals automatic priority.
- Added complete-market, data-quality, model-edge, bookmaker-depth, and independent-evidence gates for the new markets.
- Added final-active-primary settlement, decimal/Hong Kong odds handling, corner-score review inputs, primary-only statistics, and copyable plain-text formatting for every new market.
- Persist reviewed no-primary matches as machine-readable learning samples while excluding them from record, stake, profit, accuracy, and ROI.

## [2.1.0] - 2026-07-22

### Changed
- Added a persistent Japan-time lineup scheduler with a hard T−30 gate, bounded pre-kickoff retries, expiring claim leases, and invocation-time catch-up.
- Added mandatory standalone lineup tasks plus auditable completion and cleanup of match-specific Codex automations.
- Added exactly two diagnostic exact-score candidates and guarded plain-text delivery support (removed in 3.0.0).

## [2.0.0] - 2026-07-21

### Changed
- Adapted the workflow for Codex with visual output, separate T−30 lineup-check tasks, verified post-match reviews, and timezone-safe scheduling.
- Added one machine-readable active primary pick per match plus secondary formal-pick roles.
- Added primary-first accuracy/ROI statistics while retaining all-formal and legacy market statistics.
- Added guarded calibration, legacy primary migration, and tests; global weight changes remain disabled below 20 graded selections per market.

## [1.0.0] - 2026-04-13

### Added
- Initial release
- 5-step quantitative analysis framework
- Automated data scraping from titan007.com
- Asian handicap and over/under prediction models
- Dual output modes (concise / visual)
- Post-match review and accuracy tracking
- ClawHub and GitHub distribution
