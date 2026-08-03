# Expanded History and Corner Runbook

This runbook is the reproducible order for rebuilding the fourteen 2020–2026 competition
exports, their corner-result research bundle, the league corner registries, and the
football/HTFT dataset. The example cutoff is `2026-08-03`. In this context, “2020–2026”
means every verified completed fixture from 2020 through that cutoff; it does **not** mean a
complete 2026 season. Keep 2026 labelled `partial_as_of_2026-08-03` (or the matching future
cutoff) and never use a right-censored season as promotion evidence.

The examples assume three frozen schedule snapshots: the main nine competitions, four
additional competitions, and Finland Veikkausliiga. Replace the placeholders with the exact
paths for the frozen run. Use the same three files, in the same order, throughout the run.

## 1. Normalize the frozen schedules offline

Stop every collector that writes or reads these snapshots. Check all three files before any
write, then repeat the same list with `--in-place`:

```text
python -B -X utf8 scripts/titan_schedule_snapshot_normalizer.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --check
python -B -X utf8 scripts/titan_schedule_snapshot_normalizer.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --in-place
```

The normalizer performs no network requests. It validates the complete batch before atomic
replacement, adds `source_timezone`, `kickoff_utc`, and `kickoff_epoch`, maps legacy
`standard` to `regular`, and binds J1 2026 to its special competition regime. Any conflicting
pre-existing UTC/epoch value aborts the operation. Run `--check` again afterward; a second
in-place run must be a no-op.

## 2. Collect or resume verified corner results

```text
python -B -X utf8 scripts/titan_corner_history_collector.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --output-dir .codex/soccer-predict/corner-history-expanded --workers 4 --requests-per-second 4
```

The append-only checkpoint is resumable. On the first run after schedule normalization, the
collector migrates a legacy row only when its match ID, teams, result, competition, phase,
round, regime, source shape, and fixture identity agree exactly. Foreign or partially bound
checkpoint rows are ignored; J1 2026 rows with the former regime are fetched again; and
`fetch_error` rows are retried. Do not edit the checkpoint or relabel an error as complete.

Inspect both `corner_history.json` and `corner_history_qa.json` after the command exits. In
the frozen `2026-08-03` snapshot, two Ligue 1 fixtures remain `fetch_error` after retry:
`2595122` and `2800321`. Preserve those two statuses. Their corner cells stay blank in Excel
and the dataset builder excludes them; neither missing full-time nor missing half-time
corners may be filled with zero.

## 3. Collect company 8 corner-price history as research evidence

Wait for the result collector to finish before starting the price collector:

```text
python -B -X utf8 scripts/titan_corner_odds_collector.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --output-dir .codex/soccer-predict/corner-odds-expanded --company-id 8 --workers 4 --requests-per-second 4
```

This fetches the full-match corner total and corner handicap histories and retains only
source-verified snapshots strictly before the earlier trustworthy kickoff boundary. The raw
responses are content-addressed so a resumed checkpoint can be replayed instead of trusted
by its outer hash alone.

Company 8 by itself is a **single-company research sample**. It is useful for workbook audit
and historical market comparison, but it does not satisfy the three-firm diagnostic gate and
cannot validate executable consensus odds, EV, or ROI. Even a later three-company collection
does not override the current manager policy: both corner formal flags remain false until a
separate clean live-forward evaluation and a later versioned policy enable them.

## 4. Build the source-bound corner datasets

```text
python -B -X utf8 scripts/corner_history_dataset_builder.py --input .codex/soccer-predict/corner-history-expanded/corner_history.json --output-dir .codex/soccer-predict/datasets/corner-history-expanded --as-of-date 2026-08-03
```

Read the generated `manifest.json` rather than copying totals from prose. It binds the source
JSON, response evidence, fixture identities, generated league CSV hashes, cutoff, admitted
regimes/phases, and excluded cohorts. It admits only complete, regulation-time records on or
before the cutoff; errors, ambiguous extra-time phases, conflicts, missing values, and
post-cutoff rows remain visible QA exclusions. Excel is an audit/export view and is never the
authoritative corner-training input.

## 5. Train all fourteen corner leagues sequentially

`corner_model_manager.py train` updates one shared `corner-registry.json`, so never launch two
training commands against the same `--model-dir` concurrently. One PowerShell session can
read the fourteen entries from the source-bound manifest and wait for each process before
starting the next:

```powershell
$datasetDir = ".codex/soccer-predict/datasets/corner-history-expanded"
$modelDir = ".codex/soccer-predict/models/corner-history-expanded"
$bundle = Get-Content "$datasetDir/manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
foreach ($item in $bundle.leagues) {
  & python -B -X utf8 scripts/corner_model_manager.py train --input "$datasetDir/$($item.dataset_file)" --manifest "$datasetDir/manifest.json" --model-dir $modelDir --league-key $item.league_key --league $item.league
  if ($LASTEXITCODE -ne 0) { throw "corner training failed: $($item.league_key)" }
}
& python -B -X utf8 scripts/corner_model_manager.py inspect --model-dir $modelDir --output "$modelDir/inspection.json"
if ($LASTEXITCODE -ne 0) { throw "corner registry inspection failed" }
```

Do not use `ForEach-Object -Parallel`, jobs, or separate terminals writing the same registry.
`candidate` and `shadow` are historical-development labels only. Every registered entry and
prediction must retain `formal_corner_total_eligible=false` and
`formal_corner_handicap_eligible=false`; at most it can supply a validated `◇` observation.

## 6. Build final workbooks, then import football/HTFT

Only after both collectors have produced their final JSON and QA artifacts should the
external workbook exporter combine the unchanged schedules, corner results, and company 8
research prices. Export one workbook for each of the fourteen competitions. Verify the
workbook contract before copying it to the delivery directory: the competition sheet keeps
the first 87 football/HTFT columns unchanged, appends exactly the registered twelve corner
audit columns, and includes only the registered `角球盘口` and `数据质量` auxiliary sheets.
The two `fetch_error` fixtures remain blank and labelled; formulas, zero imputation, and
post-result derived training features are prohibited.

Import that final workbook directory only after all fourteen files are stable:

```text
python -B -X utf8 scripts/history_importer.py <final-workbook-directory> --output-dir .codex/soccer-predict/datasets/league-history-expanded --source-timezone Asia/Shanghai --as-of-date 2026-08-03
python -B -X utf8 scripts/htft_holdout_evaluator.py --dataset-dir .codex/soccer-predict/datasets/league-history-expanded --include-opening-market --output .codex/soccer-predict/evaluations/htft-fixed-seasons.json
python -B -X utf8 scripts/league_model_manager.py train --dataset-dir .codex/soccer-predict/datasets/league-history-expanded --model-dir .codex/soccer-predict/models/league-history-expanded --evaluation-artifact .codex/soccer-predict/evaluations/htft-fixed-seasons.json
python -B -X utf8 scripts/league_model_manager.py inspect --model-dir .codex/soccer-predict/models/league-history-expanded --output .codex/soccer-predict/models/league-history-expanded/inspection.json
```

The importer deliberately reads only the first 87 main-sheet columns for football/HTFT.
Corner outcomes and price sheets never become same-match HTFT features. The resulting 2026
cohorts remain partial-at-cutoff research/shadow evidence, and every registered HTFT artifact
also remains `formal_htft_eligible=false` until independently timestamped live-forward
evidence and the complete executable nine-way market satisfy a later policy.
