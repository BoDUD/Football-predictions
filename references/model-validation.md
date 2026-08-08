# Model Training and Forward Validation

Use this guide when fitting, replacing, or evaluating the football score model. Historical
profit is not evidence of predictive skill unless every prediction was generated before its
kickoff by the exact model version being evaluated.

## Training data contract

The baseline CSV requires:

```text
date,home_team,away_team,home_goals,away_goals
```

- `date` must include an unambiguous date or timezone-aware timestamp.
- Scores are non-negative regulation-time integers. Do not mix extra time or shootouts.
- Team names must be normalized before fitting. Duplicate matches, impossible scores,
  missing dates, and postdated corrections fail validation rather than being silently used.
- Record the source, extraction time, competition coverage, exclusions, and file hash.

Fit the reproducible baseline with:

```text
python scripts/score_model.py fit --input history.csv --output model.json \
  --half-life-days 365 --iterations 1200 --learning-rate 0.03 --regularization 0.02
```

The model uses time-decayed team attack/defence strengths, home advantage, and a
Dixon-Coles low-score correction. The current fitter jointly optimizes attack, defence, home
advantage, and rho with deterministic bounded updates; it does not silently clip the
log-rate while differentiating an unclipped objective. Its JSON artifact contains the
training window, configuration, fitted parameters, data hash, schema/model version,
deterministic model hash, convergence status, completed iterations, initial/final objective,
projected-gradient norm, boundary warnings, an independent full-parameter
projected-gradient/Armijo-backtracking fit from the same deterministic baseline, and
conditional-rho plus legacy-grid audits. The artifact records the second path's objective,
iteration/backtracking counts, gradient norm, rho/parameter distance from the primary path,
and whether its lower objective was safely adopted. A second-path improvement must exceed
the artifact's fixed `adoption_minimum_objective_improvement`; sub-tolerance numerical gains
remain an audit and cannot replace the primary vector. The second path must also independently
meet its projected-gradient convergence tolerance before adoption; a lower objective from an
unfinished short run is diagnostic only. `converged` is recomputed only from the
final selected parameter vector; a true value from an earlier Adam vector cannot survive a
later rho or full-parameter replacement by inheritance.
Treat `converged=false` or a material boundary warning as model-review evidence, not as a
reason to hide the diagnostic. Frozen artifacts from the earlier
`deterministic_adam_then_rho_grid` fitter remain readable but are not retroactively given
diagnostics they never recorded. A new cutoff or configuration creates a new artifact; never
overwrite the provenance of an archived prediction.

## Prediction contract

```text
python scripts/score_model.py predict --model model.json \
  --home-team HOME --away-team AWAY --kickoff 2030-08-10T19:00:00+09:00 \
  --output prediction.json \
  --total over:2.25 --asian home:-0.75
```

Reject the artifact unless:

- every score cell is finite and non-negative;
- matrix probability is one within tolerance and omitted tail mass is below the configured
  threshold;
- 1X2, BTTS, goal ranges, exact-score ranks, totals, and Asian settlement states reproduce
  sums from the same matrix;
- the model hash and version are present; and
- the prediction was generated and archived before the verified kickoff.

The prediction command also proves that `training.end_date` is strictly earlier than the
fixture's UTC date. Because the minimum training schema has day-level dates, same-day
training rows are rejected conservatively rather than assuming an order that was not stored.

Unknown teams fail closed by default. `--unknown-team-policy league_average` is an explicit
fallback, must produce a warning, and cannot be hidden in a high-quality formal record.

## Walk-forward evaluation

Never use a random train/test split for chronological match prediction. For each validation
cutoff:

1. Fit only on matches strictly before the cutoff.
2. Predict the next chronological block without refitting on any result inside that block.
3. Save every eligible fixture and every candidate market, including abstentions and
   rejected candidates. Do not evaluate only bets selected after seeing outcomes.
4. Advance the cutoff, refit, and repeat.

Keep a final untouched holdout period for model-selection confirmation. Compare against at
least simple home/draw/away market probabilities and a league-average Poisson baseline.

Run the bundled expanding-window evaluator without random splitting:

```text
python scripts/score_model.py backtest --input history.csv --output backtest.json \
  --min-train-matches 200 --test-block-size 50
```

It keeps complete date groups together, refits only on earlier blocks, records each block's
training cutoff/model hash, and emits deterministic prediction-level evidence plus aggregate
proper scoring rules.

The bundled evaluator is a leakage-safe baseline, not a complete promotion report. Reserve
the final holdout outside the input used for model/configuration choice, then run that holdout
once. The minimum CSV has no bookmaker prices, competition key, or lead-time field, so market
baseline/CLV comparisons and those segment cuts require an explicitly joined, timestamped
evaluation dataset. Do not infer them from the four bundled aggregate scores.

## Required metrics

Report sample size and uncertainty, overall and by model version, competition, market, and
lead-time bucket:

- multiclass log loss and Brier score for 1X2;
- ranked probability score for ordered goal totals when available;
- exact-score log loss or probability assigned to the observed score, as a diagnostic;
- calibration tables/plots by probability bin with expected and observed frequency;
- closing-line value when a comparable closing price exists;
- flat-stake profit and ROI only for strict pre-match primaries, with pushes excluded from
  the accuracy denominator but included correctly in stake/profit accounting; and
- coverage/abstention rate, because a strategy can improve apparent hit rate by selecting
  fewer matches.

Do not optimize a model on ROI alone. Prices, selection policy, and sample variance can make
ROI unstable. Promote a model only when proper scoring rules and calibration improve on the
untouched forward sample without a material data-quality regression.

## HT/FT fixed-season validation

The registered HT/FT model is fitted chronologically, not from the reviewed betting ledger.
The fixed model configuration uses a 730-day half-time decay, 365-day full-time decay,
365-day exponentially weighted Jeffreys-smoothed historical HT/FT association, and IPF calibration back to the
fixture-specific row and column marginals. Season 2025 is valid fixed model-component
evidence, but 2025 and partial 2026 were subsequently inspected while the final two-scenario
selector was developed. Neither may be described as untouched confirmation of
`probability_top2_v3_post_selection`; future clean live-forward evidence is required for an
end-to-end claim.

The association artifact records raw counts, weighted counts, effective sample weight,
reference date, formula, and half-life, and the prediction repeats that weighting audit.
The formal evaluator and registry require `association_half_life_days=365.0`; omitting it,
using uniform weights, or changing the half-life fails source-bound replay. Alternative
predeclared half-lives remain configuration experiments only and require a later versioned
policy plus chronological evidence before replacing the registered default.

Run the versioned evaluator against the importer's hash-verified local bundle:

```text
python scripts/htft_holdout_evaluator.py \
  --dataset-dir .codex/soccer-predict/datasets/league-history-expanded \
  --include-opening-market \
  --output .codex/soccer-predict/evaluations/htft-fixed-seasons.json
```

The promoted evaluator settings label 2024 `development_validation`, 2025
`model_fit_holdout_selector_development`, and 2026
`shadow_monitoring_seen_during_development`. It preserves per-fixture probability evidence
locally, reports newly promoted-team fallback separately, compares the model with a
training-only league-frequency baseline, and records fixed-seed paired uncertainty intervals.
Opening odds are emitted under a separate
`research_only_untimestamped_opening_snapshot` cohort. Any fit or bootstrap parameter outside
the promoted fixed configuration is rejected unless `--experimental-override` is passed; an
override relabels the three roles as `configuration_experiment_validation`,
`reused_holdout_configuration_experiment`, and
`reused_shadow_configuration_experiment`, and is never promotion evidence.
Even with the exact fixed settings, the artifact must retain
`model_component_evidence_only=true`, `final_selector_untouched=false`, and
`end_to_end_promotion_eligible=false` because final selector confirmation has not happened.

An evaluation hash alone is not sufficient source verification. Before interpreting a saved
artifact, call `validate_evaluation(..., dataset_dir=...)` or provide its exact manifest path;
the validator reopens the manifest-bound score and market files, verifies their hashes and
fixture identities, and then recomputes the reported metrics from prediction-level evidence.

The expanded source bundle targets nineteen competitions, including Finland Veikkausliiga,
Brazil Cup, Portugal Primeira Liga, Netherlands Eerste Divisie, the England League Cup, and
UEFA Nations League.
Do not quote a match count or model score from an older export. Read total and cohort row
counts from the validated dataset manifest, and read log loss, Brier, Top-1/Top-2,
calibration, uncertainty, fallback, and baseline deltas from the source-bound evaluation
artifact generated for that exact manifest. Right-censored 2026 research/shadow cohorts stay
visible but are excluded from `promotion_evidence`.

The registered HT/FT manager labels a league `candidate` only when the fixed 2025 holdout
contains at least 100 known-team fixtures and both log-loss and Brier deltas for that slice versus the
training-window empirical-frequency baseline have a negative mean and a negative paired-
bootstrap 95% confidence-interval upper bound. A mean-only improvement or a smaller sample
remains `shadow`.

The aggregate still mixes known-team predictions with explicitly labelled league-average
fallback predictions. The registered manager defaults to an error for unknown teams. An
explicitly requested `league_average` fallback must retain its warning and separate cohort
label; never present an aggregate containing fallback fixtures as the manager's default
known-team performance.

The model-only Top-2 probability-mass threshold and every league-specific covered/hit count
are versioned registry evidence, not a source-code table and not a confirmed confidence gate.
The ranker must accept them only when the evidence binds the exact dataset manifest,
evaluation, model hash, and league key. Because the selector was developed after inspecting
historical results and the workbooks contain no timestamped executable nine-way HT/FT
prices, `production_confidence_eligible=false` and `formal_htft_eligible=false` remain
mandatory.

A separate research experiment may use workbook opening 1X2 as a full-time marginal proxy.
Its exact sample and metrics belong only to the current evaluation artifact. These full-time
opening prices have no precise collection timestamps and are not HT/FT prices, so the cohort
is research-only. It is neither a live half-time gate, live-forward ROI, nor authorization to
make HT/FT a formal primary.

`scenario_stability_v2` is not an accepted selector. Use
`probability_top2_v3_post_selection`, with stability/coherence/score/price fields retained
only as audits. Earlier selector comparisons helped develop the policy and are not a final
untouched test; do not carry their old percentages into a newly expanded bundle. Every
evaluation artifact must record season bounds, eligible and excluded rows,
model and input hashes, configuration, prediction-level probabilities, and both overall and
per-league metrics. Never retune a threshold from the match currently being reviewed.

The importer preserves every collected Titan stage through `format_version`, `phase_group`,
`season_status`, and `competition_regime` so those cohorts remain auditable. The registered
manager follows `competition-specific-production-v3`: ordinary leagues accept only
`competition_regime=regular`, Brazil Cup and the England League Cup accept
`national_knockout_cup`, and UEFA Nations
League accepts `national_team_league_and_knockout`. Each competition is fitted separately;
other regimes are not silently merged, and only their excluded-row counts and drift warnings flow into the registry.
This is not an independent production model for every phase. Deployment status is derived
from the current fixed holdout and must be read from the hash-bound registry rather than
hard-coded by league name. Every registered model is non-formal, and every unfinished 2026
cohort remains research/shadow regardless of its interim metric.

## Corner-model validation boundary

Corner-count training is a separate lineage from the football score and HT/FT models. The
corner manager must bind the exact verified 90-minute corner-history CSV, its fitted model,
and a chronological walk-forward backtest. Read the actual sample counts and proper scores
from those artifacts; do not infer corner quality from the football-goal manifest, a prose
trend, or a workbook percentage.

The current corner manager may assign only historical `candidate` or `shadow` status. Both
`formal_corner_total_eligible` and `formal_corner_handicap_eligible` are deliberately false,
because an historical backtest is not an independent strict live-forward test. A future
manager version must bind prediction-time fixtures, executable current corner prices,
abstentions, calibration, settlement, and model/market hashes before either formal flag may
be enabled. Until then, corner output is a `◇` observation even when diagnostic EV is
positive and the current two-way market is complete.

## Forward candidate-evaluation cohort

New normal initial and lineup-check archives must not discard a market merely because its
release policy is paused or because no formal primary is ultimately selected. Build one
fixture-bound `soccer_candidate_evaluation` artifact with schema
`candidate-evaluation/3.0.0`, pass it with `--candidate-evaluation-file`, and add
`--require-candidate-evaluations`. The complete manifest covers `asian`, `total`,
`half_time`, `htft`, `goal_range`, `btts`, `corner_total`, and `corner_handicap`; mark each
market either `evaluated` with at least one candidate or `unavailable` with a concrete reason.
The opt-in flag preserves compatibility for already frozen historical calls, but the Skill's
forward workflow must use it. Legacy `candidate-evaluation/2.0.0` remains historical read-only
and cannot authorize an active cohort write. Never construct this artifact after kickoff or
backfill it into an old archive.

For each evaluated candidate, include its market identity, executable price and odds format,
complete mutually exclusive current market, source and timezone-aware collection time,
consensus/median basis, bookmaker count, market-signal class, canonical five-state settlement
distribution, and probability. Optional EV, no-vig market probability, and edge values are
assertions only: `memory_store.py` recalculates them. Football probabilities are reopened from
the validated joint scenario; corner probabilities require a matching validated corner
observation. Fixture, cutoff, freshness, probability, complete-market, and artifact hashes are
validated before archive. Artifact generation must be no earlier than every market snapshot
and every joint/corner model or ranker generation time it consumes; an impossible timeline is
rejected rather than downgraded to a failed gate.

Candidate gates are separated into `integrity`, `value`, `risk`, and `release`. A candidate is
`counterfactual_eligible` only when every non-release gate passes. Release-policy failures do
not remove it from the shadow cohort, which breaks the former circular dependency where a
paused market could never accumulate the forward evidence needed for review. At most one
counterfactual candidate per match and market is deterministically marked `shadow_selected`;
it remains an observation with no stake, monetary settlement, primary result, accuracy, or
ROI. `formal_eligible` still requires every gate, including release gates, and the evaluation
artifact never creates or promotes a formal primary by itself.

The public publication summary is a read-only projection of that validated audit. It may show
one cross-market `observation_primary` only when the selected candidate is both
`shadow_selected=true` and `counterfactual_eligible=true`, remains
`formal_eligible=false`, and the archived version has no formal primary. The cross-market
tie-break reuses the frozen shadow confidence, settlement-safety, bookmaker-depth, and stable
identity ordering. It never edits the candidate artifact, fills a formal-pick field, creates a
stake, or changes settlement. Failed gates are exposed separately as data, value, policy, or
mixed safety blockers; the original machine gate category and reason remain available so the
display taxonomy cannot be mistaken for a changed release rule. When no candidate passes all
non-release gates, the publication summary reports `no_usable_direction` and must not expose a failed
near-miss as an observation.

The archive freezes the original source payload, its canonical hash, and an active-version
binding covering fixture, stage, archive time, joint/score lineage, data quality, and guardrail
evidence. The reviewed settlement basis freezes the same evaluation context. Before review or
statistics, the validator replays canonical distributions, no-vig values, five-state EV/edge,
all gates, confidence components, ranks, and shadow selections from those inputs and requires
an exact match. It also recomputes the post-match diagnostic from the verified result. Rewriting
derived fields and recalculating the audit's self-hash is therefore invalid. Aggregate shadow
statistics deduplicate by frozen match and market, not artifact byte hash, so whitespace or key
ordering cannot manufacture additional samples.

Post-match review settles the frozen shadow selection against verified regulation-time,
half-time, or corner results as applicable. `stats` and `calibrate` expose the per-market shadow
selection cohort and a release-blocker funnel separately from primary performance. Twenty
graded shadow selections in one market is only a manual model/policy validation trigger;
`parameter_change_authorized` remains false, market status is unchanged, and no automatic
release, threshold change, or historical rewrite is permitted.

They also expose recent 50- and 100-match candidate-gate windows as diagnostic coverage, not
performance evidence. Each window selects distinct matches by their latest immutable pre-match
archive time, then replays every initial and lineup-check v3 candidate audit belonging to those
matches. Pending records are included because the purpose is to diagnose collection and release
friction before settlement. Missing historical v3 audits, rejected replay, and explicitly
unavailable markets remain counted as coverage gaps; they are never backfilled from the current
model. Stage, market, raw gate, and blocker-type denominators are reported independently, and an
incomplete 50/100 window must say so rather than implying a full sample.

## Untouched live-forward confirmation

Historical holdouts and the candidate/shadow ledger are development evidence; neither is the
final confirmation sample. Before the first fixture in a confirmation run, commit the reviewed
code and freeze one complete `forward-policy/3.0.0` manifest. The manifest binds the Git commit
and hashes of every prediction-affecting source file, the dataset manifest and its declared hash,
the model registry and training configuration, market statuses, candidate selector, release
thresholds, evidence freshness, and public display policy. `freeze` requires the caller to name
the reviewed final merge commit, verifies that it is the current clean `HEAD`, and refuses an
intermediate feature-branch commit or dirty worktree. Both freeze and start require the same
explicit cohort kind. The current runtime permits only `local-integrity-shadow-v2`, which starts
a `live-forward-cohort/2.0.0` after that final policy exists; do not start a cohort on the review
branch:

```bash
python -m scripts.forward_policy --base-dir <workspace> --repo-root <repo> freeze \
  --dataset-manifest <football-dataset-manifest.json> \
  --model-registry <football-model-registry.json> \
  --corner-dataset-manifest <corner-dataset-manifest.json> \
  --corner-model-registry <corner-model-registry.json> \
  --cohort-scope-file <cohort-scope.json> \
  --expected-final-merge-commit <final-merge-git-sha> \
  --cohort-kind local-integrity-shadow-v2

python -m scripts.forward_policy --base-dir <workspace> --repo-root <repo> start \
  --policy-file <forward-policy.json> \
  --cohort-id <stable-cohort-id> \
  --cohort-kind local-integrity-shadow-v2

python scripts/memory_store.py --base-dir <workspace> close-forward-cohort \
  --cohort-id <stable-cohort-id> \
  --closed-at <timezone-aware-ISO>
```

`forward-policy/3.0.0` is the only active policy schema. It hash-binds the complete current
confirmation contract, including `cohort_kind`, and active validation compares every runtime
selector, threshold, market-status, display, and validation-protocol field with the installed
runtime. A resealed local variant is not an active policy. `forward-policy/2.0.0` is the previous,
kind-less provenance schema and `forward-policy/1.0.0` is the earlier legacy schema: both remain
structurally replayable, but neither may be frozen again, started, or extended with a new
observation commitment. Policy files used by the current runtime are content-addressed direct
children of `.codex/soccer-predict/forward-policies/`; their filename must be
`<policy_id>.json`.

`live-forward-cohort/2.0.0` is the separate current cohort schema. It hash-binds its top-level
`kind`, and that kind must equal the policy's `confirmation_contract.cohort_kind`.
`live-forward-cohort/1.0.0` is historical read-only. Do not confuse policy v2 with cohort v2:
their version sequences are independent. `local-integrity-shadow-v2` establishes local
Git/hash/replay integrity only; its report must carry an assurance blocker and cannot be described
as promotable confirmation. Cohort IDs are portable ASCII identifiers, not paths; separators,
dot traversal, whitespace, non-ASCII text, trailing dots, and Windows device names are rejected.

`promotable-confirmation-v2` is deliberately unavailable and fails closed at both policy freeze
and cohort start until four real adapters exist: an external trusted timestamp anchor, replayable
baseline artifacts, replayable executable entry-price sources, and replayable closing-price
sources. These are mandatory promotion conditions, not optional metadata, and caller-supplied
flags cannot replace them.

Every record in an active cohort must be archived after `starts_at` and receives the complete
policy snapshot and cohort hash. Current `forward-policy-binding/3.0.0` (or committed
`3.1.0`) carries `forward-provenance-binding/2.0.0` and binds `package_version` (derived from
`soccer_predict.__version__`), `git_commit_sha`, `policy_hash`, `validation_config_hash`,
`dataset_manifest_hash`, `model_registry_hash`, `renderer_policy_hash`, `cohort_id`,
`cohort_kind`, `assurance_scope=local_integrity_only`, and
`promotion_evidence_eligible=false`. The renderer identity covers the display policy plus the
public-outlook, prediction-card, review-card, and plain-text formatter source hashes. Previous
`forward-policy-binding/2.0.0`/`2.1.0` with `forward-provenance-binding/1.0.0` remains
replayable only with `forward-policy/2.0.0`; its historical
`untouched_confirmation_eligible=true` flag means pre-kickoff integrity eligibility under that
old contract, not promotion eligibility. It cannot receive a new commitment. Prediction-affecting
code, data, model, selector, threshold,
market-status, or display-policy changes invalidate the frozen binding and require a new cohort;
close the old cohort explicitly, preserve its immutable manifest and closure artifact, and never
rewrite it. Non-predictions, abstentions, and unavailable markets are all part of the denominator.
Old fixtures and the previous 25 selections remain useful quarantined history, but cannot be
inserted into this untouched cohort.

Before any fixture analysis, build a versioned `forward-cohort-scope/1.0.0` and append exactly
one `requested` event for that user-requested fixture. The NDJSON event log is append-only and
hash-chained. A request may end only as a frozen archive record or an explicit terminal
`unavailable` event. At closure, the record manifest's fixture set and each record's
`request_event_hash` must reproduce the event log exactly. This defines the estimand as distinct
user-requested fixtures instead of the subset that happened to produce usable odds.

The policy's `forward-artifact-lineage/1.1.0` has separate roles for football history, corner
history, football HT/FT models, and corner models. File hashes alone are insufficient: validation
rebuilds both registries from disk, checks their dataset links, and freezes every registered
league's model hash (plus the football full-time component or corner dataset hash). A candidate
cannot substitute an unregistered model from the same directory or another registry.

An active cohort also requires `source-evidence/2.0.0`. Export the visible pre-kickoff page state
to JSON, including fixture identity, exact kickoff, source URL, collection time, HTTP metadata,
and complete per-company outcome prices. Build and replay the bundle with:

```bash
python scripts/source_evidence.py build \
  --source-file <visible-page-export.json> \
  --output-dir <workspace>/.codex/soccer-predict/source-evidence

python scripts/source_evidence.py verify \
  --evidence <workspace>/.codex/soccer-predict/source-evidence/<match-id>-source-evidence.json
```

The synthetic, explicitly non-evidence contract example is
[`analysis/fixtures/visible_market_snapshot.json`](../analysis/fixtures/visible_market_snapshot.json).
It documents the adapter input shape but must never be copied into a real cohort.

The builder keeps each raw JSON response under a content-addressed path, records its byte hash,
HTTP metadata and parser version, and derives median/consensus prices only from complete firm
rows. `memory_store.py record --source-evidence-file ...` replays that raw evidence and requires
each candidate's market, timestamp, odds format, complete outcome prices, price basis, and firm
count to reproduce. A derivative-only JSON file, edited raw response, incomplete market,
mismatched fixture, or post-kickoff timestamp fails closed. These local hashes provide a durable
audit boundary for `local-integrity-shadow-v2`; an external trusted timestamp must not be claimed
until an external service is actually configured, and it remains a mandatory prerequisite for
`promotable-confirmation-v2` rather than an optional promotion enhancement. Legacy
`source-evidence/1.0.0` is historical read-only and cannot satisfy an active cohort.

Current active records additionally require `fundamental-evidence/1.0.0`. Build it from one or
more saved visible pre-kickoff exports and pass it with `--fundamental-evidence-file`. Replay
derives the six evidence gates (confirmed lineups, injuries, chance quality, attacking
configuration, opponent-tail check, and corner profile) from content-addressed sources; command
line booleans cannot unlock these gates. This adapter preserves raw exported JSON plus HTTP
metadata, but it is not a generic raw-HTTP-body collector. That limitation, external timestamping,
and the missing independent closing snapshot keep the cohort local shadow only.

Market consensus and execution are separate evidence objects. Median/no-vig firm rows define the
market baseline. A ledger row claiming an actual entry must instead bind
`execution-offer-evidence/1.0.0`: one named firm and account region, exact market/selection,
quote and accepted timestamps, quoted and accepted decimal odds, max stake, requested stake, and
acceptance status. Replay rejects a stake above the limit, an improved post-hoc price, a fixture or
market mismatch, or a derivative-only execution assertion. Missing execution remains an explicit
evaluation blocker; it is never inferred from the consensus median.

If the visible source contains no market table, archive a source snapshot with
`availability_status=unavailable`, an empty market list, and at least one concrete reason. This
keeps the fixture in the cohort denominator without inventing prices; the candidate manifest must
then mark the affected markets unavailable rather than silently dropping the match.

New confirmation evidence uses `forward-observations/3.0.0`. The defective v2 contract is
explicitly rejected and must remain isolated. V1 remains readable as historical read-only and may
produce descriptive statistics from its frozen rows, but its local-integrity and promotion gates
always fail; no v1 result can authorize a policy or parameter change. V3 has three separate
layers. First freeze a
complete eligibility queue containing each unique `(cohort_id, fixture_id,
market_identity_hash)` before any listed kickoff. The identity hash is reproduced from the
canonical `{family, period, line, price_outcomes}` object, so two lines or outcome spaces within
one market family cannot collapse into one queue key. Then archive one canonical pre-match
prediction payload for every queue key, including predicted, abstained and unavailable states,
and bind its hash into provenance-complete `forward-policy-binding/3.1.0`. The prediction payload
contains no result. An active-cohort `memory_store.py record` must receive this already validated,
all-pending micro-ledger with `--forward-validation-ledger`; the record operation atomically
archives its content hash, market-specific commitments, canonical record-prediction payload,
v3.1 binding, and archive-version hash. It never reconstructs missing baselines or a queue from
post-match history. Initial and lineup-check versions therefore receive distinct commitments.
Finally add a separate settlement that binds the commitment hash and contains the verified result
and optional closing snapshot. The evaluator requires exact queue coverage, one commitment and one
explicit settled-or-pending settlement per key; caller-chosen duplicate observation IDs,
retrospective generation, late market snapshots and post-result edits to model probabilities fail
closed. Close the cohort only through `memory_store.py close-forward-cohort`. It holds the history
lock while selecting every bound record in the named cohort, writes a canonical
`live-forward-record-manifest/1.0.0`, embeds that manifest in the
`live-forward-cohort-closure/2.0.0`, and closes the active pointer before releasing the lock. Each
canonically sorted manifest entry binds the fixture ID, archive-version hash, record-commitment
hash, committed-binding hash, and pre-match-ledger hash. The lower-level `forward_policy.py close`
requires this manifest explicitly; a preview manifest followed by a later close is not the formal
workflow because another record could enter between those operations.

Current v3 evaluation also requires a canonical memory-store cohort export carrying
`memory-forward-history-ledger-binding/3.0.0`. Formal export requires the
`live-forward-cohort-closure/2.0.0` file and has no fixture filter: it always selects every history
record bound to the named cohort. Its canonically ordered receipt list contains one
`memory-forward-record-receipt/2.0.0` per manifest entry. Every
receipt embeds the replayed micro-ledger and immutable archive snapshot, and reproduces the
pre-match ledger hash, archive-version hash, record commitment, committed policy binding, archived
time, and exact market commitments. The evaluator first requires a closed cohort, then compares
those four receipt anchors with the independently closure-bound manifest before aggregating rows. A
naked v2 payload, open cohort, caller-supplied SHA wrapper, or selected fixture subset cannot enter
formal evaluation. Deleting, copying, reordering, replacing, truncating, or retrofitting a receipt
fails closed even if the attacker recomputes the aggregate binding hash and fixture list. Legacy
v1 closures and binding schemas 1.x, plus the previous policy-v2 binding schemas 2.x, remain
readable for historical inspection only, appear solely in explicit defect-quarantine summary
metadata, and cannot enter the current formal export, confirmation metrics, or promotion gate. Local
content hashes still cannot prove wall clock time against an attacker who can reseal the entire
local chain, so the external timestamp promotion blocker remains mandatory.

Each market now declares its own outcome schema, so 1X2, HT/FT and five-state return distributions
cannot accidentally share one top-level outcome list. Model baselines carry generated time,
training cutoff and artifact hash inside the pre-match commitment. Same-time bookmaker
probabilities are only an assertion: the validator reopens the content-addressed source-evidence
bundle, reproduces the complete decimal prices, and recomputes multiplicative no-vig probabilities
as `(1 / odds_i) / sum(1 / odds)`. The snapshot must be no later than the binding archive time and
within the policy-frozen decision-time tolerance. Executable entry and closing snapshots retain
complete markets; CLV uses their no-vig selection probabilities, and returns use win, half-win,
push, half-loss and loss settlement states rather than a hit/miss shortcut.

For `asian` and `corner_handicap`, `market_identity.line` is always written from the home-side
perspective. Thus Away +0.75 and Home -0.75 refer to the same quoted market identity with
`line=-0.75`; `selection` and `settlement_reference_outcome` identify which side is evaluated.
Never flip the line sign merely because the selected side is away.

The validation protocol itself is frozen in the policy: bootstrap seed and repetitions, minimum
paired samples, minimum independent ISO-week clusters, segment requirements, same-time tolerance
and calibration threshold. CLI overrides are explicitly experimental and block the gate. Generate
the report with:

```bash
python scripts/memory_store.py --base-dir <workspace> export-forward-validation \
  --cohort-id <cohort-id> \
  --cohort-closure-file <workspace>/.codex/soccer-predict/forward-cohorts/<cohort-id>-closure.json \
  --output <forward-observations.json>

python scripts/forward_validation.py \
  --input <forward-observations.json> \
  --output <forward-validation.json>
```

The report calculates paired log loss and multiclass Brier deltas, pooled and per-outcome
calibration/ECE, selection coverage, model availability, abstention and unavailable rates,
league/market/lead-time segments, five-state flat-stake and actual stake-weighted ROI, no-vig CLV,
and kickoff-ISO-week clustered bootstrap intervals. It reports missing outcome, baseline and
execution IDs instead of silently dropping them, and a single-week bootstrap can never satisfy the
minimum-cluster gate. Local content hashes are tamper-evident workflow controls, not a third-party
timestamp. Until a real external timestamp anchor is configured, the report keeps
`external_timestamp_anchor_not_configured` as a blocker. For the only currently constructible
kind, `local-integrity-shadow-v2`, that assurance blocker makes the derived
`promotion_eligible=false`. The field is calculated from integrity, assurance, statistical, and
manual gates rather than hard-coded, but `promotable-confirmation-v2` cannot be frozen or started
until its required adapters exist, so no current report can legitimately make it true.
`parameter_change_authorized` remains false; the evaluator never changes model
parameters, thresholds, market policy, or old archives.

The current local memory-store shadow also has no independent pre-kickoff closing-snapshot replay
adapter. A real local export therefore retains execution-source, CLV, and overall statistical
blockers; it must not advertise an end-to-end statistical pass from caller-supplied closing data.
The five-state model-space proper-score subgate can still be evaluated honestly against its frozen
historical-frequency, independent-HT/FT, and simple-Poisson/DC baselines. For categorical markets,
bookmaker proper scores are evaluated only when the bookmaker and model share the exact canonical
outcome space. For split-line five-state markets, bookmaker prices are limited to price-space EV
and CLV and are never treated as five-state proper-score probabilities.

When rebuilding all nineteen competitions, train them sequentially into one registry and run one
final `inspect`; concurrent `train` commands must not write the same `registry.json`. The
source-normalization, collection, dataset, and handoff commands are in
[expanded-history-runbook.md](expanded-history-runbook.md).

## Ledger cohorts

Statistics must expose separate cohorts:

- `strict_oos`: immutable, generated and archived before kickoff with auditable current
  market data and model provenance;
- `legacy`: historical records that predate the strict schema;
- `backfill`: records or primaries assigned after the original prediction time;
- `invalid_timing`: any archive at or after kickoff or with an unresolved timezone; and
- `force_or_rewrite`: any legacy record whose active prediction was overwritten.

Only `strict_oos` may be used to claim current forward performance. Preserve other cohorts
for learning and forensic review, but never blend them into a headline win rate or ROI.
Quarantine is not deletion: verified fixture/result rows may still enter a chronological
training dataset, while backfilled pick labels, post-kickoff probabilities, and contaminated
profit outcomes must not become training targets or model-selection evidence.
