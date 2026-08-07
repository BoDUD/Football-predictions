# Expanded History and Corner Runbook

This runbook is the reproducible order for rebuilding the nineteen 2020–2026 competition
exports, their corner-result research bundle, the league corner registries, and the
football/HTFT dataset. The example cutoff is `2026-08-07`. In this context, “2020–2026”
means every verified completed fixture from 2020 through that cutoff; it does **not** mean a
complete 2026 season. Keep 2026 labelled `partial_as_of_2026-08-07` (or the matching future
cutoff) and never use a right-censored season as promotion evidence.

The examples assume six frozen schedule snapshots: the original main and additional sets,
Finland Veikkausliiga, the Brazil Cup/UEFA Nations League set, and the Portugal Primeira
Liga/England League Cup set, plus Netherlands Eerste Divisie. Replace the placeholders
with the exact paths for the frozen run. Use the same files, in the same order, throughout.

## 1. Normalize the frozen schedules offline

Stop every collector that writes or reads these snapshots. Check all files before any
write, then repeat the same list with `--in-place`:

```text
python -B -X utf8 scripts/titan_schedule_snapshot_normalizer.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --schedule <run-root>/data_cups/schedules.json --schedule <run-root>/data_portugal_efl/schedules.json --schedule <run-root>/data_netherlands/schedules.json --check
python -B -X utf8 scripts/titan_schedule_snapshot_normalizer.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --schedule <run-root>/data_cups/schedules.json --schedule <run-root>/data_portugal_efl/schedules.json --schedule <run-root>/data_netherlands/schedules.json --in-place
```

The normalizer performs no network requests. It validates the complete batch before atomic
replacement, adds `source_timezone`, `kickoff_utc`, and `kickoff_epoch`, maps legacy
`standard` to `regular`, and binds J1 2026 to its special competition regime. Any conflicting
pre-existing UTC/epoch value aborts the operation. Run `--check` again afterward; a second
in-place run must be a no-op.

## 2. Collect or resume verified corner results

```text
python -B -X utf8 scripts/titan_corner_history_collector.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --schedule <run-root>/data_cups/schedules.json --schedule <run-root>/data_portugal_efl/schedules.json --schedule <run-root>/data_netherlands/schedules.json --output-dir .codex/soccer-predict/corner-history-expanded --workers 4 --requests-per-second 4
```

The append-only checkpoint is resumable. On the first run after schedule normalization, the
collector migrates a legacy row only when its match ID, teams, result, competition, phase,
round, regime, source shape, and fixture identity agree exactly. Foreign or partially bound
checkpoint rows are ignored; J1 2026 rows with the former regime are fetched again; and
`fetch_error` rows are retried. A non-empty fallback response that deterministically disagrees
with fixture identity or terminal state is frozen once as `conflicting`, with both header and
fallback URL/hash/error evidence, and is not retried. Transport/timeout/empty-response failures
remain `fetch_error`. Do not edit the checkpoint or relabel either status as complete.
Collector v1.1 also copies the schedule `raw_tail` into every row and includes it in
`schedule_fixture_sha256`. A v1.0 checkpoint can be enriched offline only when all legacy
identity and source-evidence fields still agree. If replaying an England League Cup tail
changes its saved extra-time classification, that row is fetched again instead of being
silently promoted. The immutable result-exclusion policy rejects England League Cup IDs
`1927696` and `2044807`, both unplayed administrative ties, plus Netherlands Eerste
Divisie ID `2871575`, a match permanently stopped in the 88th minute, before corner
collection. The latter is a non-regulation result rather than an administrative walkover.

Inspect both `corner_history.json` and `corner_history_qa.json` after the command exits.
Preserve every `fetch_error` or `conflicting` status reported by the frozen artifact. Their corner cells stay blank in Excel
and the dataset builder excludes them; neither missing full-time nor missing half-time
corners may be filled with zero.

For the England League Cup, keep the whole source stage as `knockout` and the source regime
as `national-knockout-cup`. A Titan raw-tail `;;1,` extra-time marker makes the corner period
unsafe and excludes the row from fitting; a direct penalty shootout without that marker is
not extra time, so its verified regulation-time corner count remains eligible. Football and
corner targets always use FT90, never the extra-time or shootout result.

## 3. Collect company 8 corner-price history as research evidence

Wait for the result collector to finish before starting the price collector:

```text
python -B -X utf8 scripts/titan_corner_odds_collector.py --schedule <run-root>/data/schedules.json --schedule <run-root>/data_extra/schedules.json --schedule <run-root>/data_finland/schedules.json --schedule <run-root>/data_cups/schedules.json --schedule <run-root>/data_portugal_efl/schedules.json --schedule <run-root>/data_netherlands/schedules.json --output-dir .codex/soccer-predict/corner-odds-expanded --company-id 8 --workers 4 --requests-per-second 4
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
python -B -X utf8 scripts/corner_history_dataset_builder.py --input .codex/soccer-predict/corner-history-expanded/corner_history.json --output-dir .codex/soccer-predict/datasets/corner-history-expanded --as-of-date 2026-08-07
```

Read the generated `manifest.json` rather than copying totals from prose. It binds the source
JSON, response evidence, fixture identities, generated league CSV hashes, cutoff, admitted
regimes/phases, and excluded cohorts. It admits only complete, regulation-time records on or
before the cutoff; errors, ambiguous extra-time phases, conflicts, missing values, and
post-cutoff rows remain visible QA exclusions. For current v1.1 sources the builder recomputes
the raw-tail-bound fixture hash and independently replays the England League Cup extra-time
classification. Legitimate v1.0 source artifacts remain readable and can rebuild their
original pre-v1.1 competition set, but they cannot claim Portugal, the England League Cup,
or Netherlands Eerste Divisie.
Excel is an audit/export view and is never the authoritative corner-training input.

## 5. Train all nineteen corner competitions sequentially

`corner_model_manager.py train` updates one shared `corner-registry.json`, so never launch two
training commands against the same `--model-dir` concurrently. Manager 2.2 deliberately rejects
the previous registry contract. Therefore the first 3.5 training command must target a new path
that does not already exist; do not point it at the live 2.1.1 registry. One PowerShell session
can read the nineteen entries from the source-bound manifest and wait for each process before
starting the next:

```powershell
$datasetDir = ".codex/soccer-predict/datasets/corner-history-expanded"
$cornerStagingModelDir = ".codex/soccer-predict/models/corner-history-expanded-3.5.0-staging"
if (Test-Path -LiteralPath $cornerStagingModelDir) {
  throw "corner staging model directory already exists; preserve it for audit and choose a new empty staging path"
}
$bundle = Get-Content "$datasetDir/manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($bundle.leagues).Count -ne 19) { throw "corner manifest is not the validated 19-competition bundle" }
foreach ($item in $bundle.leagues) {
  & python -B -X utf8 scripts/corner_model_manager.py train --input "$datasetDir/$($item.dataset_file)" --manifest "$datasetDir/manifest.json" --model-dir $cornerStagingModelDir --league-key $item.league_key --league $item.league
  if ($LASTEXITCODE -ne 0) { throw "corner training failed: $($item.league_key)" }
}
& python -B -X utf8 scripts/corner_model_manager.py inspect --model-dir $cornerStagingModelDir --output "$cornerStagingModelDir/inspection.json"
if ($LASTEXITCODE -ne 0) { throw "corner registry inspection failed" }
```

Do not use `ForEach-Object -Parallel`, jobs, or separate terminals writing the same registry.
`candidate` and `shadow` are historical-development labels only. Every registered entry and
prediction must retain `formal_corner_total_eligible=false` and
`formal_corner_handicap_eligible=false`; at most it can supply a validated `◇` observation.

## 6. Build final workbooks, then import football/HTFT

Only after both collectors have produced their final JSON and QA artifacts should the
external workbook exporter combine the unchanged schedules, corner results, and company 8
research prices. Export one workbook for each of the nineteen competitions. Verify the
workbook contract before copying it to the delivery directory: the competition sheet keeps
the first 87 football/HTFT columns unchanged, appends exactly the registered twelve corner
audit columns, and includes only the registered `角球盘口` and `数据质量` auxiliary sheets.
Every `fetch_error` fixture in the current frozen artifact remains blank and labelled;
use the artifact's own count for each rebuild. Formulas, zero imputation, and
post-result derived training features are prohibited.

Import that final workbook directory only after all nineteen files are stable. Fit the HT/FT
registry into its own new staging directory for the same reason: the v3 regime policy rejects the
old live registry and must never be mixed with it.

```powershell
$historyDatasetDir = ".codex/soccer-predict/datasets/league-history-expanded"
$evaluationFile = ".codex/soccer-predict/evaluations/htft-fixed-seasons.json"
$htftStagingModelDir = ".codex/soccer-predict/models/league-history-expanded-3.5.0-staging"
$workbookDirectory = "C:\path\to\final-workbooks"
if (Test-Path -LiteralPath $htftStagingModelDir) {
  throw "HTFT staging model directory already exists; preserve it for audit and choose a new empty staging path"
}
& python -B -X utf8 scripts/history_importer.py $workbookDirectory --output-dir $historyDatasetDir --source-timezone Asia/Shanghai --as-of-date 2026-08-07
if ($LASTEXITCODE -ne 0) { throw "history import failed" }
$historyBundle = Get-Content "$historyDatasetDir/manifest.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($historyBundle.leagues).Count -ne 19) { throw "history manifest is not the validated 19-competition bundle" }
& python -B -X utf8 scripts/htft_holdout_evaluator.py --dataset-dir $historyDatasetDir --include-opening-market --output $evaluationFile
if ($LASTEXITCODE -ne 0) { throw "HTFT evaluation failed" }
& python -B -X utf8 scripts/league_model_manager.py train --dataset-dir $historyDatasetDir --model-dir $htftStagingModelDir --evaluation-artifact $evaluationFile
if ($LASTEXITCODE -ne 0) { throw "HTFT registry training failed" }
& python -B -X utf8 scripts/league_model_manager.py inspect --model-dir $htftStagingModelDir --output "$htftStagingModelDir/inspection.json"
if ($LASTEXITCODE -ne 0) { throw "HTFT registry inspection failed" }
```

The importer reads the first 87 main-sheet columns for football/HTFT and reads the appended
`Titan比赛ID` only as immutable fixture identity for duplicate and immutable-result
checks. Corner outcomes and price sheets never become same-match HTFT features. The resulting 2026
cohorts remain partial-at-cutoff research/shadow evidence, and every registered HTFT artifact
also remains `formal_htft_eligible=false` until independently timestamped live-forward
evidence and the complete executable nine-way market satisfy a later policy.

## 7. Back up and switch only validated staging registries

Do not copy staging files over a live registry and do not delete the old model trees. Re-run both
semantic inspections immediately before switching. The following uses same-volume directory
renames, preserves unique backups, and rolls the old directory back if installing its replacement
fails. Stop prediction processes that are actively reading these directories before the switch.

```powershell
$backupTag = Get-Date -Format "yyyyMMdd-HHmmss"
$runtimeRoot = [IO.Path]::GetFullPath((Join-Path (Get-Location).Path ".codex/soccer-predict"))
$modelRoot = [IO.Path]::GetFullPath((Join-Path $runtimeRoot "models"))
$archiveBatchRoot = [IO.Path]::GetFullPath((Join-Path $runtimeRoot "model-archives/$backupTag"))

function Assert-ExactChildPath {
  param([string]$Path, [string]$Parent, [string]$ExpectedLeaf)
  $full = [IO.Path]::GetFullPath($Path)
  $expected = [IO.Path]::GetFullPath((Join-Path $Parent $ExpectedLeaf))
  if (-not [String]::Equals($full, $expected, [StringComparison]::OrdinalIgnoreCase)) {
    throw "refusing unexpected model-tree path: $full (expected $expected)"
  }
  return $full
}

$cornerStagingModelDir = Assert-ExactChildPath (Join-Path $modelRoot "corner-history-expanded-3.5.0-staging") $modelRoot "corner-history-expanded-3.5.0-staging"
$cornerLiveModelDir = Assert-ExactChildPath (Join-Path $modelRoot "corner-history-expanded") $modelRoot "corner-history-expanded"
$htftStagingModelDir = Assert-ExactChildPath (Join-Path $modelRoot "league-history-expanded-3.5.0-staging") $modelRoot "league-history-expanded-3.5.0-staging"
$htftLiveModelDir = Assert-ExactChildPath (Join-Path $modelRoot "league-history-expanded") $modelRoot "league-history-expanded"
$cornerBackupModelDir = Assert-ExactChildPath (Join-Path $archiveBatchRoot "corner-history-expanded") $archiveBatchRoot "corner-history-expanded"
$htftBackupModelDir = Assert-ExactChildPath (Join-Path $archiveBatchRoot "league-history-expanded") $archiveBatchRoot "league-history-expanded"

foreach ($requiredTree in @($cornerStagingModelDir, $cornerLiveModelDir, $htftStagingModelDir, $htftLiveModelDir)) {
  if (-not (Test-Path -LiteralPath $requiredTree -PathType Container)) {
    throw "safe paired switch requires an existing model tree: $requiredTree"
  }
}
if (Test-Path -LiteralPath $archiveBatchRoot) {
  throw "archive batch already exists; choose a new backup tag: $archiveBatchRoot"
}
[void][IO.Directory]::CreateDirectory($archiveBatchRoot)

& python -B -X utf8 scripts/corner_model_manager.py inspect --model-dir $cornerStagingModelDir --output "$cornerStagingModelDir/pre-switch-inspection.json"
if ($LASTEXITCODE -ne 0) { throw "corner staging registry failed pre-switch inspection" }
& python -B -X utf8 scripts/league_model_manager.py inspect --model-dir $htftStagingModelDir --output "$htftStagingModelDir/pre-switch-inspection.json"
if ($LASTEXITCODE -ne 0) { throw "HTFT staging registry failed pre-switch inspection" }
$cornerRegistry = Get-Content "$cornerStagingModelDir/corner-registry.json" -Raw -Encoding UTF8 | ConvertFrom-Json
$htftRegistry = Get-Content "$htftStagingModelDir/registry.json" -Raw -Encoding UTF8 | ConvertFrom-Json
if (@($cornerRegistry.leagues).Count -ne 19) { throw "corner staging registry is not complete: expected 19 entries" }
if (@($htftRegistry.leagues).Count -ne 19) { throw "HTFT staging registry is not complete: expected 19 entries" }
& python -B -X utf8 scripts/corner_model_manager.py verify-integrity --model-dir $cornerStagingModelDir --output "$cornerStagingModelDir/pre-switch-integrity.json"
if ($LASTEXITCODE -ne 0) { throw "corner staging registry failed bounded integrity verification" }
& python -B -X utf8 scripts/league_model_manager.py verify-integrity --model-dir $htftStagingModelDir --output "$htftStagingModelDir/pre-switch-integrity.json"
if ($LASTEXITCODE -ne 0) { throw "HTFT staging registry failed bounded integrity verification" }

function Install-ValidatedModelTree {
  param([string]$Staging, [string]$Live, [string]$Backup, [string]$ModelsRoot, [string]$ArchivesRoot)
  if (-not [String]::Equals([IO.Path]::GetDirectoryName($Staging), $ModelsRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "staging path escaped models root: $Staging" }
  if (-not [String]::Equals([IO.Path]::GetDirectoryName($Live), $ModelsRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "live path escaped models root: $Live" }
  if (-not [String]::Equals([IO.Path]::GetDirectoryName($Backup), $ArchivesRoot, [StringComparison]::OrdinalIgnoreCase)) { throw "backup path escaped archive batch: $Backup" }
  if (-not (Test-Path -LiteralPath $Staging -PathType Container)) { throw "missing staging model tree: $Staging" }
  if (-not (Test-Path -LiteralPath $Live -PathType Container)) { throw "missing live model tree: $Live" }
  if (Test-Path -LiteralPath $Backup) { throw "backup target already exists: $Backup" }
  $movedLive = $false
  try {
    if (Test-Path -LiteralPath $Live -PathType Container) {
      Move-Item -LiteralPath $Live -Destination $Backup -ErrorAction Stop
      $movedLive = $true
    }
    Move-Item -LiteralPath $Staging -Destination $Live -ErrorAction Stop
  } catch {
    if ($movedLive -and -not (Test-Path -LiteralPath $Live) -and (Test-Path -LiteralPath $Backup)) {
      try {
        Move-Item -LiteralPath $Backup -Destination $Live -ErrorAction Stop
      } catch {
        throw "install and automatic rollback both failed; preserve and inspect Live=$Live Backup=$Backup Staging=$Staging"
      }
    }
    throw
  }
}

function Restore-PreviousModelTree {
  param([string]$Staging, [string]$Live, [string]$Backup)
  try {
    if (Test-Path -LiteralPath $Staging) { throw "rollback staging target is occupied: $Staging" }
    if (-not (Test-Path -LiteralPath $Live -PathType Container)) { throw "new live tree is missing: $Live" }
    if (-not (Test-Path -LiteralPath $Backup -PathType Container)) { throw "previous live backup is missing: $Backup" }
    Move-Item -LiteralPath $Live -Destination $Staging -ErrorAction Stop
    Move-Item -LiteralPath $Backup -Destination $Live -ErrorAction Stop
  } catch {
    throw "automatic pair rollback failed; stop all predictions and inspect Live=$Live Backup=$Backup Staging=$Staging before any manual move"
  }
}

$cornerInstalled = $false
try {
  Install-ValidatedModelTree -Staging $cornerStagingModelDir -Live $cornerLiveModelDir -Backup $cornerBackupModelDir -ModelsRoot $modelRoot -ArchivesRoot $archiveBatchRoot
  $cornerInstalled = $true
  Install-ValidatedModelTree -Staging $htftStagingModelDir -Live $htftLiveModelDir -Backup $htftBackupModelDir -ModelsRoot $modelRoot -ArchivesRoot $archiveBatchRoot
} catch {
  $pairFailure = $_
  if ($cornerInstalled) {
    Restore-PreviousModelTree -Staging $cornerStagingModelDir -Live $cornerLiveModelDir -Backup $cornerBackupModelDir
  }
  throw $pairFailure
}
```

Each rename is atomic on one filesystem; the two registries are intentionally switched one after
the other with rollback protection, not represented as one cross-directory transaction. Backups
live under `model-archives`, outside doctor's canonical `models` scan, and must be preserved. Only
after the final merged code and these installed registries are frozen together
may a new forward policy/cohort begin.
