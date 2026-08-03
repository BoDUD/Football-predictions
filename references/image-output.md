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

The stage is identified in the title/subtitle, not by changing the table structure. Each row
must bind `archive_match_id`, `archive_stage`, and `archive_version_hash`. The renderer selects
that exact initial or lineup-check version, recomputes its hash, and verifies kickoff time,
league, and teams. It must not silently read whichever revision is active today or reuse a
lineup artifact in an initial card.

The compact cells follow these display rules:

- `主推` shows the single archived formal primary that ranked highest after all active gates,
  with the archive-derived `★`. If no direction qualified, show `无正式推荐`; do not promote a
  model observation merely to fill the cell.
- `总进球` shows only the highest-probability goal range from the validated path posterior,
  together with its probability and lead over the second-ranked range. Do not show a second
  goal-range choice in the image.
- `半全场` and `波胆` are two views of the same ranked joint path events. Items at the same
  position are one inseparable `(HT/FT × full-time score)` event and share its genuine joint
  probability. Never display independently ranked HT/FT and score lists next to each other.
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
fallback pairs.

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
must bind the final active pre-match settlement basis, show the verified half-time and final
scores, the official primary settlement state, the archived paired joint-event references,
and the learning/statistics context required by the review contract. It may adapt the column
labels for review facts, but it must retain the compact table style, full-text layout, and the
same no-truncation rule. It must never rewrite the pre-match recommendation after seeing the
result.

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
- `◇` means a structured observation passed its diagnostic qualification audit. It remains a
  non-primary reference and must not be described as a bet.
- `无正式推荐` means no executable direction qualified. It never receives a star.
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
