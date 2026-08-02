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
  --dataset-dir .codex/soccer-predict/datasets/league-history \
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

Across Brazil Serie A, Japan J1, Norway Eliteserien, and MLS, the fixed 2025 component cohort
contains 1,510 matches. The model produced multiclass log loss 1.92022, Brier 0.82267, Top-1 accuracy
30.07%, and two-scenario coverage accuracy 48.48%. The training-only empirical-frequency
baseline was worse (log loss 1.94199, Brier 0.83160, Top-1 28.54%, Top-2 43.05%). A
fixed-seed, league-stratified paired bootstrap for the model versus that baseline gave mean
log-loss change -0.02177 with 95% interval [-0.03260, -0.01049], and Brier change -0.00893
with interval [-0.01277, -0.00524].

That overall cohort includes two materially different production-availability groups. The
known-team group has 1,370 matches (log loss 1.91190, Top-2 49.42%); the league-average
fallback group has 140 matches (log loss 2.00162, Top-2 39.29%). The registered production
manager defaults to an error for unknown teams. An explicitly requested
`league_average` fallback must retain its warning and separate cohort label; do not present
the 1,510-match aggregate as the manager's default production performance.

The global 0.46 model-only Top-2 probability-mass threshold is descriptive development
evidence, not a confirmed confidence gate. The 2025 league cohorts are:

| League | Eligible | Covered | Hits | Hit rate | Wilson 95% lower bound |
| --- | ---: | ---: | ---: | ---: | ---: |
| Brazil Serie A | 380 | 125 | 72 | 57.60% | 48.84% |
| Japan J1 | 380 | 66 | 32 | 48.48% | 36.85% |
| Norway Eliteserien | 240 | 110 | 63 | 57.27% | 47.94% |
| MLS | 510 | 208 | 114 | 54.81% | 48.02% |

All four Wilson lower bounds are at or below 50%. The ranker therefore uses `league_key` to
display the matching evidence but cannot issue a league-confirmed confidence label; missing
or unsupported league context remains unconfirmed. A separate research experiment using
workbook opening 1X2 as a full-time marginal anchor produced 2025 log loss 1.90470 and Brier
0.81813; the 0.50 gate covered 320 matches (21.19%) with 61.56% two-scenario accuracy. Those
full-time opening prices have no precise collection timestamps, so `0.50` and `61.56%` are
research-only. They are neither a live half-time gate, live-forward ROI, nor authorization to
make HT/FT a formal primary.

`scenario_stability_v2` is not an accepted selector: it reduced 2025 Top-2 accuracy from
48.48% to 45.70%. Its older partial-2026 comparison mixed the Japan special regime and is
not a formal metric. Use
`probability_top2_v3_post_selection`, with stability/coherence/score/price fields retained
only as audits. The comparison itself helped develop the selector and is not a final untouched
test. Every evaluation artifact must record season bounds, eligible and excluded rows,
model and input hashes, configuration, prediction-level probabilities, and both overall and
per-league metrics. Never retune a threshold from the match currently being reviewed.

The production manager follows `regular-only-production-v1`: it trains only rows marked
`competition_regime=regular`. All 180 supplied Japan J1 2026 fixtures are labelled
`2026_vision_regional`, excluded from registered production fitting and formal fixed-season
metrics, with only excluded-row counts retained as competition-regime-drift evidence. The complete 9,211-match bundle covers only
Brazil Serie A, Japan J1, Norway Eliteserien, and MLS. It provides no validated transfer
benefit for Korean K League, so Korean predictions stay on the generic observation path and
must not cite these metrics.

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
