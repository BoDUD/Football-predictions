# Changelog

## [3.9.0] - 2026-08-08

- Bind current cohort closure to the immutable cohort/policy identity, enforce monotonic fixture-event causality through archive time, and version the request/denominator/record-manifest contract without rewriting historical artifacts.
- Add registered fundamental source adapters, identity-derived candidate markets, and an explicit five-match minimum for directional shadow evidence; formal release gates remain unchanged.
- Separate Chinese competition display labels from canonical model/registry keys for all 19 trained competitions.

## [3.8.0] - 2026-08-08

- Hardened active forward cohorts with full fixture-bound request events, explicit reschedule/replacement transitions, strict archive ordering, verified model-unavailable dispositions, and cohort-wide firm receipt uniqueness.
- Added candidate-directional, source-classed fundamental evidence as a versioned shadow contract; conflicting confirmed lineups and unrecognized starters now fail closed without promoting the new rule to formal release.
- Added semantic model-manager verification receipts to artifact lineage and separated accepted firm execution prices from decision-time consensus no-vig probabilities.

Released-version entries below describe historical behavior and are superseded by newer entries when policies change.

## [Unreleased]

## [3.7.1] - 2026-08-08

### Fixed
- Reproduce the canonical corner dataset builder and model manager artifacts when freezing role-aware lineage: `forward-artifact-lineage/1.2.0` now validates the manifest's `dataset_sha256` field and the registry's list-shaped 19-league entries instead of synthetic fixture-only shapes, while historical `1.1.0` snapshots remain replayable.

## [3.7.0] - 2026-08-08

- Freeze a complete, role-aware football/HTFT and corner data/model lineage instead of a single registry pair.
- Add an immutable user-request denominator with pre-analysis requests, terminal unavailable dispositions, and closure reconciliation.
- Bind replayable fundamental claims and real firm-specific execution offers separately from market consensus prices.
- Require every recorded denominator fixture to replay the exact pre-analysis request event and every candidate model to match the exact per-league hash frozen in its role-specific registry.
- Keep raw-HTTP capture, external timestamping, independent closing snapshots, evaluation-identity redesign, and broader calibration upgrades explicitly out of scope; `local-integrity-shadow-v2` remains non-promotable.
- Clarify that the historical `confidence_score` is a heuristic stability-ranking score, not a calibrated probability.
- Add focused Python 3.13 and 3.14 provenance/evidence compatibility smoke jobs while retaining the full 3.11/3.12 suites.

## [3.6.2] - 2026-08-08

### Fixed
- Made lineup-check plain text and prediction cards compare observation transitions against the same frozen initial archive, without changing the existing immediate-revision baseline used for formal-primary price arrows.
- Rendered every non-formal `主推` table cell and the no-primary footer explanation in the neutral no-bet color while preserving the observation color exclusively for the separate non-counting observation panel.

## [3.6.1] - 2026-08-08

### Fixed
- Restored historical replay of frozen pre-3.6 forward-policy manifests by validating their provenance and renderer hashes against the protected-file contract that existed when they were created.
- Kept 3.6-and-later active policies fail-closed on the expanded protected-file set; historical compatibility cannot reactivate an older package policy or omit current provenance files.

## [3.6.0] - 2026-08-08

### Added
- Added a replay-derived, non-counting observation-primary view for archived `candidate-evaluation/3.0.0` records, with explicit data, value, policy, and safety blockers while keeping every formal-primary gate unchanged.
- Added recent 50/100-match candidate-gate funnel diagnostics across immutable initial and lineup-check versions, including archive coverage, unavailable markets, stage splits, and raw failure reasons.

### Changed
- Kept the eight-column prediction table and formal-primary cell unchanged, while adding a separate observation/publication-status panel and stage-specific initial versus T−30 messaging to image and plain-text output.
- Require any active live-forward cohort to close before these display semantics change; a replacement cohort may start only after this release's final merge commit is frozen.

## [3.5.0] - 2026-08-07

### Added
- Expanded the auditable 2020–2026 football/HTFT and regulation-time corner history contract to nineteen competitions by adding Portugal Primeira Liga, the England League Cup, and Netherlands Eerste Divisie.
- Added immutable England League Cup walkover exclusions and a distinct non-regulation exclusion for the 88th-minute Cambuur–Vitesse termination, plus season-specific format labels, competition-regime boundaries, Chinese display mappings, Titan source identity, and fixed schedule-completeness expectations.

### Changed
- Advanced the history dataset schema to 1.5.0, the workbook importer, HT/FT evaluator regime policy, corner dataset selection policy, and corner manager provenance so prior competition-set artifacts fail closed and must be rebuilt before use. History schema 1.4.0 remains an explicitly rejected historical boundary because its required exclusion-policy field has a different structure.
- Advanced the corner collector contract to v1.1 by hash-binding replayable schedule `raw_tail`, independently replaying England League Cup extra-time classification in the dataset builder, and rejecting every immutable administrative or non-regulation result exclusion in collection and construction. Legacy v1.0 source artifacts remain readable for their original competitions, but cannot claim the three newly added competitions.
- Made non-empty deterministic Titan fallback disagreements terminal `conflicting` QA rows with both header and fallback URL/hash/error evidence; only transport failures and empty responses remain retryable `fetch_error` rows, and neither status can enter corner training.
- Any HT/FT or corner model later trained from the expanded contract remains formally ineligible pending clean live-forward evidence; this release note does not claim that local registries have already been rebuilt, or that betting accuracy or ROI improved.

## [3.4.0] - 2026-08-07

### Added
- Added a frozen forward-policy manifest and untouched live-forward cohort contract that bind code, data, model, selector, gate, market-status, and display-policy identities without fabricating promotion evidence from historical or reviewed matches.
- Added executable source-evidence envelopes with raw payload hashes, parser versions, offline replay, fixture binding, and schema canaries for provider adapters.
- Added an installable `soccer-predict` command with a side-effect-safe `doctor` covering Python, optional raster support, Chinese fonts, timezone data, workspace state, model artifacts, and scheduler/watchdog health; external connectivity remains explicit opt-in.
- Added categorized test inventory, clean-wheel smoke installation, Windows and PowerShell watchdog coverage, and real notebook execution against an explicitly non-evidence CI fixture.
- Expanded the audited history and corner-training contract to sixteen competitions by adding Brazil Cup and UEFA Nations League 2020–2026 workbooks, competition-specific regimes, cross-calendar edition handling, and administrative-result exclusions.

### Changed
- Added the explicit `local-integrity-shadow-v2` and `promotable-confirmation-v2` policy kinds. Active policy manifests are now `forward-policy/3.0.0`; `forward_policy.py freeze` and `start` require a matching `--cohort-kind`, and the independently versioned `live-forward-cohort/2.0.0` hash-binds that kind. Kind-less policy v2/v1 and cohort v1 artifacts remain historical read-only.
- Advanced active record provenance to `forward-policy-binding/3.0.0`/`3.1.0` and `forward-provenance-binding/2.0.0`, replacing the ambiguous untouched boolean with hash-bound cohort kind, local assurance scope, and explicit non-promotion eligibility. Previous binding 2.x/provenance 1.0 records remain structurally replayable only in defect quarantine and cannot be extended or formally exported.
- Advanced active forward artifacts to `candidate-evaluation/3.0.0`, `source-evidence/2.0.0`, and `forward-observations/3.0.0`. Legacy candidate v2, source v1, and observations v1 remain historical read-only; the defective observations v2 contract is explicitly rejected and isolated. None can authorize active cohort writes or promotion.
- Separated bookmaker `price_outcomes` from model `settlement_states`, bound every queue/source/candidate/commitment/settlement row to the canonical family/period/line/outcome identity, and limited bookmaker proper scores to genuinely comparable categorical outcome spaces. Split-line Asian, total, and corner markets now retain two-way no-vig prices for EV/CLV while their five-state distributions remain the settlement and return space.
- Replaced the two-stage Dixon–Coles fit with a jointly bounded attack/defence/home/rho optimizer, retained deterministic legacy cross-checks, and exposed convergence, objective, gradient, iteration, and boundary diagnostics. Formal HT/FT evaluation and registration now freeze the association half-life at 365 days and reject uniform, missing, or drifted association weighting.
- Added joint box-and-zero-sum projection for sparse corner-team effects, corrected CRPS scoring outside retained support, and raised the still-bounded corner-tail audit ceiling so knockout-cup histories can train without invalid parameter or tail truncation.
- Renamed and expanded the public joint-scenario uncertainty display so the Rank-1-derived goal band, independent goal-range audit, Top-2 cumulative mass, remaining mass, and entropy state cannot be mistaken for certainty or independently paired selections.
- Hardened one-time scheduling with exact UTC/date-bound schedule specifications and returned next-run verification across day, month, DST, and past-hour boundaries.
- Extracted pure probability, settlement, policy, validation, and source-evidence boundaries from monolithic workflow code while preserving frozen archive and settlement behavior.
- Expanded Ruff lint and format checks to all Python package, script, and test files, while keeping current coverage and typing gates explicit about their measured scope.

### Security
- Kept `promotable-confirmation-v2` unavailable and fail-closed until external trusted timestamp, baseline-artifact replay, executable entry-price source replay, and closing-price source replay adapters exist. Local hashes and caller assertions cannot satisfy those promotion requirements.
- Pinned GitHub Actions to immutable commit SHAs and kept all new release, validation, and display changes fail-closed; no market was promoted and no historical artifact was rewritten.
- Replaced caller-supplied history SHA wrappers with canonical per-record memory-store receipts and a replayed cohort export; formal v3 evaluation rejects naked payloads, forged timestamps, and deleted, duplicated, reordered, or replaced receipts.
- Bound formal cohort exports to a history-locked `live-forward-cohort-closure/2.0.0` containing the complete canonical record manifest, so selected fixture subsets and receipt-list shrinkage remain invalid even after outer aggregate hashes are recomputed.
- Split historical policy verification from active-runtime enforcement so future package upgrades can replay an internally consistent older cohort without allowing that older policy to create or continue an active cohort.
- Made cohort closure crash recovery idempotent: an already written byte-equivalent closure/record manifest can complete the closed pointer on retry, while any differing content fails closed and leaves the original closure unchanged.
- Required every source-visible replayable market and quoted price outcome to appear in the frozen candidate denominator, and bound any formal primary to the exact eligible candidate, source, model, signal, confidence, execution price, and ledger commitment. Missing result or closing-price replay now quarantines the observation from formal scoring, settlement, CLV, and promotion with explicit assurance blockers.

## [3.3.0] - 2026-08-06

### Added
- Added a fixture-bound lineup fallback contract that checks official competition/club sources, public ESPN lineup pages, and exact-event Sofascore pages when Titan lacks either starting XI, while keeping predicted, non-official, or conflicting lineups out of `lineup_confirmed` evidence.
- Added live-fetched, page-hash-bound Titan competition metadata so the user-facing Chinese tournament name is separated from an internal proxy-model league key.
- Added a complete-analysis archive guard for normal initial and lineup-check predictions.
- Added archive-to-card regression coverage for explicit no-primary output, one Rank-1-score-derived goal range, and frozen joint Top-2 HT/FT-score pairs.
- Added fixture-bound `candidate-evaluation/2.0.0` audits for all supported markets, deterministic per-market shadow selection, verified post-match shadow settlement, and separate shadow/release-blocker calibration funnels.
- Candidate audits now reject generation before any consumed market/model snapshot, freeze and replay their source plus active-version evidence binding, recompute verified-result diagnostics, deduplicate shadow samples by match/market, and use half-stake-weighted edge probabilities for split settlements.

### Changed
- Missing Titan lineup data now triggers one bounded multi-source fallback pass instead of an immediate "unavailable" conclusion; a still-unconfirmed lineup completes the scheduled check with lineup-dependent gates closed rather than causing a retry or fabricated XI.
- Public scenario output now shows one total-goal range mapped deterministically from the frozen global joint-event Rank 1 score, plus exactly the frozen global joint-event Top 2 in descending joint-probability order across initial, lineup, and review text/cards. Every paired row keeps its HT/FT label and exact score inseparably aligned, repeated HT/FT labels remain visible when their scores differ, and neither a third event nor a repeated Rank 2 goal-range label is displayed. Independent 1X2/goal-range marginals, HT/FT Top 2, unconditional exact-score Top 2, and hit ranks remain audit-only.
- Render no-formal-primary cards as exactly `无正式主推`; never promote a marginal 1X2 or goal-range leader into the primary or joint slots. Separately qualified observations remain non-counting text/audit annotations and do not occupy the card primary.
- Allow a newer renderer to project the one visible goal range from Rank 1 of an old valid frozen joint artifact without rewriting the archive, artifact, probabilities, order, settlement basis, or archive hash; missing or invalid joint artifacts still fail all three public projection cells closed as `数据不足`.
- Keep integrity/value/risk gates independent from release gates so paused markets can accumulate clean forward shadow evidence without becoming formal picks; twenty graded shadows triggers manual validation only and never auto-releases a market or authorizes parameter changes.
- Make review-card provenance wording reflect whether the frozen settlement version actually contains a validated joint path.
- Derive card date/title/stage from the archive, reject mixed stages/dates and duplicate matches, and freeze competition identity into settlement/statistics.
- Upgrade legacy reviewed settlement bases through an explicit audited migration so later top-level league changes cannot alter review cards or statistics; missing historical competition evidence remains explicitly unavailable.
- Reject market-direction or odds-shaped text in caller-controlled fixture metadata and account for wide Latin glyphs when enforcing fixed-card width limits.
- Allow missing half-time results in review images only when the settlement contract permits them, displaying `未取得` instead of failing rendering.

## [3.2.0] - 2026-08-03

### Added
- Expanded the audited 2020–2026 history contract to fourteen competitions, including Finland Veikkausliiga, with 2026 retained as a partial-at-cutoff research cohort.
- Added source-bound regulation-time corner-result and pre-kickoff corner-price collectors, offline schedule normalization, league dataset generation, chronological corner models, registered replay validation, and fixed untouched holdouts.
- Added a deterministic daily prediction-card renderer that derives `★`, `◇`, and no-primary status from the archived active record instead of caller-supplied labels.
- Added a feasible score-path/IPF artifact that produces genuine `(HT/FT × full-time score)` joint probabilities while reproducing the canonical full-time score matrix and registered HT/FT marginal.
- Added immutable joint-artifact archiving with fixture, timestamp, model-lineage, input-hash, convergence, tail-mass, and tamper validation.
- Added a compact four-dimensional path kernel with complete Hall support-feasibility auditing and reconstruction-based HT/second-half/FT/HTFT/derived-market validation.
- Added a shared `public_market_outlook` layer that keeps complete half-time, 1X2, goal-range and BTTS distributions with probability-gap audits while producing the compact public selections from the same posterior.
- Added deterministic post-match review images bound to the final active settlement basis and verified result.

### Changed
- Hardened HT/FT evaluation and registration around fixed known-team holdouts, paired baseline uncertainty, dataset/model/evaluation lineage, and fail-closed non-formal deployment flags.
- Kept corner totals, corner handicaps, and HT/FT observation-only until separate clean live-forward evidence enables a later policy; historical `candidate` status never authorizes a formal pick.
- Preserved legacy and no-primary review wording and learning records while keeping them outside primary win rate, stake, profit, and ROI.
- Replaced independent user-facing HT/FT and exact-score lists with globally ranked joint path events. The display normally uses two paired events and adds a third only when the versioned complexity rule identifies a divided head distribution; 1X2, total-goal counts, goal ranges, and BTTS come from the same validated artifact or fail closed as `数据不足`.
- Standardized initial and lineup-check images on the same simple eight-column table (`编号`, `时间`, `赛事`, `主队 vs 客队`, `主推`, `总进球`, `半全场`, `波胆`). Total goals now show only the top range and its probability lead, while complete distributions remain in text/audit.
- Removed ellipsis-based image truncation. Long titles, team names, cells, legends, and review wording now use wrapping, readable font reduction, row growth, or canvas expansion.
- Bound structured Titan market evidence to the exact fixture and pre-kickoff collection time. Until a versioned market-fusion method passes strict forward calibration, those prices remain diagnostic and cannot be double-counted as both conditioning input and independent EV proof.
- Removed standalone HT/FT observations from the poster's observation marker; only a separately validated eligible observation model, such as the current corner diagnostic, may receive `◇`.
- Bound every poster row to an exact archive stage and version hash, verified its fixture time/league/teams, and blocked cross-stage artifact leakage, unqualified corner observations, and free-form recommendation/notes from becoming public directions.
- Enforced 60-minute initial and 30-minute lineup-check market-evidence TTLs, plus impossible half-time/full-time score rejection during review.

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
