# Prediction image and text output

Use this contract for every user-requested daily slate and, by default, for a single-match
initial prediction, lineup check, or completed post-match review. The user-facing result
contains one deterministic image plus concise analytical text. Do not repeat the same content
under a `可复制纯文本版` heading unless the user explicitly requests a copyable block.

## Image renderer

Build a UTF-8 JSON payload from the exact archived version and bind it to the workspace
prediction archive:

```bash
python scripts/prediction_card_renderer.py \
  --input slate.json \
  --history .codex/soccer-predict/history.json \
  --output slate.png
```

SVG is the dependency-free output. PNG/JPEG output uses Pillow and a real Chinese font; if a
raster dependency or Chinese font is unavailable, render SVG instead of substituting broken
glyphs. Save generated files under the ignored `.codex/soccer-predict/rendered/` directory and
display the image from its absolute local path.

## Initial and lineup-check card

Initial and lineup-check results use the same compact eight-column table, in this exact order:

1. `编号`
2. `时间`
3. `赛事`
4. `主队 vs 客队`
5. `主推`
6. `总进球`
7. `半全场`
8. `波胆`

The stage is identified in the title/subtitle, not by changing the table structure. Date,
title, and subtitle are derived by the renderer from the bound archive and must not appear in
the caller payload. Each row must bind `archive_match_id`, `archive_stage`, and
`archive_version_hash`. The renderer selects that exact initial or lineup-check version,
recomputes its hash, and verifies kickoff time, competition, teams, and the source-bound
metadata. It must not silently read whichever revision is active today or reuse a lineup
artifact in an initial card. One image cannot mix archive stages, local kickoff dates, or two
versions of the same match; make separate cards instead.

Caller-supplied row status is only an assertion: formal primary, qualified observation, or
no-bet priority is derived from the selected archive and a mismatch is rejected. Duplicate
`archive_match_id` rows are rejected so one fixture cannot inflate the displayed formal
count by appearing once as initial and again as lineup-check. When source-verified competition metadata exists, the row must also
bind its exact `competition_evidence_hash`; only then may the renderer replace an internal
proxy model league key with the verified Chinese competition label.

Every normal initial and lineup-check revision is a complete-analysis archive. Before the
archive call, generate and validate the fixture-bound joint-scenario artifact. Record it with
both `--joint-scenario-file <joint-scenarios.json>` and `--require-complete-analysis`; never
create a partial normal archive and attach joint data in a later step. Failure to produce a
valid artifact before kickoff means the workflow fails closed and reports `数据不足`.

The compact cells follow these display rules:

- `主推` shows the single archived formal primary that ranked highest after all active gates,
  with the archive-derived `★`. If complete joint analysis is valid but no formal direction
  qualifies, it may instead show the validated joint artifact's `one_x_two.top1` as
  `◇ 模型首选（不计战绩）`. This renderer-derived reference never enters a formal pick field:
  assign no stake and exclude it from win/loss, profit, and ROI. Do not manufacture an
  observation merely to fill the cell.
- `总进球` shows only the highest-probability goal range from the validated path posterior,
  together with its probability and lead over the second-ranked range. Do not show a second
  goal-range choice in the image.
- `半全场` and `波胆` are compact projections of the same ranked joint path events. In
  `半全场`, list every distinct HT/FT label once in first-appearance order without a rank
  prefix or percentage. In `波胆`, retain every selected path's full-time score and genuine
  joint probability in ranked order. Never repeat an HT/FT label, sum only the displayed
  paths into a fake HT/FT probability, or independently rank the two columns.
- Normally display the top two paired joint events. Display three only when the versioned
  distribution-complexity rule identifies a genuinely divided top cluster. The system makes
  this choice from probabilities; callers and prose cannot request, suppress, reorder, or
  hand-pick the third event.

The image is intentionally concise, but the analysis is not. Half-time marginals, full-time
1X2, complete goal-range distribution, BTTS, market movement, EV/edge, evidence coverage, and
other supported markets remain fully calculated and available in accompanying text or the
machine-readable audit. Omitting them from the eight-column image must not omit them from the
model or archive.

## Joint-path integrity

The payload references the archived joint-posterior artifact, not caller-supplied HT/FT or
score arrays. A displayed pair must be aggregated from actual match paths. Never multiply an
HT/FT marginal by a score marginal, replace a score to match a desired full-time direction,
or fill a missing event from notes, an older revision, or user preference.

The artifact must prove fixture and version identity, generation before kickoff, training
cutoff, normalization, tail quality, and agreement between its score, HT/FT, 1X2, totals,
goal-range, and BTTS marginals. Market prices may condition the posterior only under a
versioned method supported by strict forward calibration. A price used for conditioning
cannot also be claimed as independent EV evidence against that posterior. If the artifact or
any required validation fails, render `数据不足` for the affected cells and no fabricated
fallback pairs. `数据不足` describes missing or invalid joint analysis; it must not be used
merely because the formal recommendation gate rejected every direction. With a valid joint
artifact, keep the genuine total and paired scenarios visible and label an eligible model
choice `◇ 模型首选（不计战绩）` when no formal primary exists.

For an immutable historical initial or lineup revision that lacks an accepted joint artifact,
keep `数据不足`. Never reconstruct, backfill, or rewrite its joint scenarios from independent
HT/FT and exact-score lists, notes, another revision, the final score, or any post-match
evidence. A newer renderer may change presentation, but it must not invent analysis that was
not frozen before kickoff.

## Historical archives and old Codex tasks

Updating the repository does not rewrite an old Codex message, image, or archived prediction.
Before continuing an old task, update or reinstall the active `soccer-predict` Skill from the
same committed repository revision and verify that its `SKILL.md`, renderer scripts, and
references match that revision. A new turn in the old task may then invoke the current
renderer, but every previously sent message and image remains a historical artifact. Save a
rerender under a new filename; do not overwrite the earlier image and imply that it existed
before kickoff.

Apply these archive-state rules:

- A historical `initial` or `lineup-check` revision without a validated joint artifact may be
  rerendered only as `数据不足`. Do not attach or synthesize a joint artifact later.
- A pending legacy `initial` revision is immutable. The archive does not allow
  `initial -> initial` replacement. If the match is still pre-kickoff, the only normal upgrade
  is a fresh, complete `initial -> lineup-check` transition inside the verified T-30 window,
  with a newly generated fixture-bound joint artifact. After kickoff, no such transition is
  allowed.
- A `reviewed` record is terminal. Do not fetch and settle it again. Rerender an initial or
  lineup card from that exact archived revision, and render the review from its immutable
  `settlement_basis`. If the settlement basis has no validated joint artifact, the review
  remains `数据不足` even if a later mutable top-level field contains joint-looking data.
- Before rendering statistics or reviews from a legacy reviewed archive whose
  `settlement_basis` predates frozen competition fields, run
  `python scripts/memory_store.py --base-dir <workspace> migrate-settlement-basis --write`
  once. The migration snapshots the preserved top-level source/league identity into the
  settlement basis and records an explicit migration audit; it writes
  `competition_evidence: null` when no evidence was frozen at settlement and never fetches or
  invents historical evidence. Until this explicit migration runs, the reviewed competition
  fails closed as `赛事待核验` instead of falling back to mutable top-level metadata.
- Competition evidence may be appended only while a pre-match record is still pending.
  Reviewed records are terminal, and their competition/statistics identity comes from the
  frozen settlement basis. The append does not change model probabilities, picks, scenarios,
  settlement, or revision history, but it deliberately changes `archive_version_hash`
  because the visible competition identity is now part of the version binding.

For a source-verified competition correction, use only a competition registered by the
archive. The command itself fetches the matching Titan analysis page, verifies both header
teams, derives the visible competition label/ID/link from that page, and stores a SHA-256 of
the fetched HTML plus its ETag/Last-Modified metadata. The caller-supplied label, ID, and link
are assertions, not the source of truth. Run once without `--write` within five minutes of the
supplied collection time and inspect `prediction_fields_unchanged: true`, the page hash, and
the before/after archive hashes:

```bash
python scripts/memory_store.py --base-dir <workspace> attach-competition-evidence \
  --match-id 2991125 \
  --competition-key brazil_cup \
  --competition-label 巴西杯 \
  --competition-id 186 \
  --verification-source https://zq.titan007.com/analysis/2991125cn.htm \
  --source-locator //info.titan007.com/cup_match/2026-2027/cupmatch_vs/cupmatch_186.htm \
  --collected-at 2026-08-04T20:21:09+09:00
```

Repeat the verified command with `--write` to persist it. The command returns an
`evidence_hash` and a new `archive_version_hash_after`. A new prediction-card payload must use
that after-hash, may keep the archived raw/model league as an identity assertion, and must
also bind the evidence hash. Do not put `date`, `title`, or `subtitle` in this JSON; the
renderer derives them:

```json
{
  "archive_version_hash": "sha256:<archive-version-hash-after>",
  "league": "巴西杯",
  "competition_evidence_hash": "sha256:<returned-evidence-hash>"
}
```

The renderer rejects a missing, forged, conflicting, unregistered, or wrong-fixture evidence
binding. Attaching display metadata can correct a visible competition label; it cannot turn an
old incomplete prediction into a complete current-model forecast.

Corners remain outputs of a separate corner model. Keep them in accompanying text or a clearly
separate audit panel; do not attach a corner direction to a goal/HTFT path without a separately
versioned and strictly validated cross-market joint model.

## No-truncation layout rule

User-visible images must contain no ellipsis character or three-dot truncation. Do not crop a
cell and append a placeholder. Preserve the full selected content by wrapping at semantic
boundaries, reducing font size down to the documented readable minimum, increasing row height,
or expanding the canvas. Apply the same rule to titles, subtitles, footers, team names, market
labels, probabilities, and all review wording. Rendered-image tests must check both the absence
of truncation markers and the final bounding boxes.

## Post-match review card

Every completed review also generates a deterministic image in the same visual family. It
must bind the final active pre-match settlement basis, show the verified final score and the
verified half-time score when available, the official primary settlement state, and the
archived paired joint-event references. When half-time evidence was not required for
settlement and could not be verified, show `未取得`; do not infer it from the final score. A
half-time or HT/FT primary still requires a verified half-time score and must remain pending
without one.

The accompanying normal text carries the causal learning and league/cumulative statistics
required by the review contract. The image may adapt the column labels for review facts, but
it must retain the compact table style, full-text layout, and the same no-truncation rule. It
must never rewrite the pre-match recommendation after seeing the result.

For a reviewed match whose final active version had no primary, keep this exact wording in the
image and accompanying text:

```text
主推：无正式推荐（不结算、不计战绩）
```

Render a completed review directly from the immutable history record; do not accept a
caller-authored review payload:

```bash
python scripts/review_card_renderer.py \
  --history .codex/soccer-predict/history.json \
  --match-id 2913681 \
  --output .codex/soccer-predict/rendered/2913681-review.png
```

## Marker semantics

- `★` means one unique archived, policy-enabled formal primary passed every active model,
  market, data-quality, evidence, and timing gate. A slate may have multiple starred matches,
  but never more than one starred direction per match.
- `◇` means either a separately archived, diagnostically qualified observation or the
  renderer-derived 1X2 leader from an accepted joint artifact. Display the latter as
  `模型首选（不计战绩）`; both remain non-primary, have no stake or settlement, and must not
  be described as a bet.
- `无正式推荐` means no executable direction qualified. It never receives a star. When a valid
  `◇` model choice is shown, retain the explicit no-formal-primary and non-counting status.
- `数据不足` means the required joint artifact is absent or invalid. It is not a synonym for
  no formal recommendation.
- Reject caller-supplied markers, formal rows without an archived primary, and observations
  that failed their diagnostic qualification.

## Accompanying text

After the image, provide compact normal text that adds the analysis hidden by the compact card:
complete distributions, current market provenance, recalculated EV/edge, evidence quality,
failed gates, lineup effects, and tail risks. Do not claim a high win rate, guaranteed return,
or profit from a prediction. The image changes presentation only; it does not change primary
counting, settlement, profit/ROI, market eligibility, or archive requirements.

The renderer writes a local file only. It has no WeChat sender, chat-client automation,
account configuration, clipboard delivery, or external-message side effect.
