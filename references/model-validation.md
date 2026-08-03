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
Dixon-Coles low-score correction. Its JSON artifact contains the training window,
configuration, fitted parameters, data hash, schema/model version, and deterministic model
hash. A new cutoff or configuration creates a new artifact; never overwrite the provenance
of an archived prediction.

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
Jeffreys-smoothed historical HT/FT association, and IPF calibration back to the
fixture-specific row and column marginals. Season 2025 is valid fixed model-component
evidence, but 2025 and partial 2026 were subsequently inspected while the final two-scenario
selector was developed. Neither may be described as untouched confirmation of
`probability_top2_v3_post_selection`; future clean live-forward evidence is required for an
end-to-end claim.

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

The expanded source bundle targets fourteen competitions, including Finland Veikkausliiga.
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
manager follows `regular-only-production-v1`: it trains only rows marked
`competition_regime=regular`; special regimes are not silently merged into regular team
strengths, and only their excluded-row counts and drift warnings flow into the registry.
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

When rebuilding all fourteen leagues, train them sequentially into one registry and run one
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
