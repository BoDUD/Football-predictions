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
