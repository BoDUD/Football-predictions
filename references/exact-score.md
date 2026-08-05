# Exact-Score Internal Audit and Joint Scenarios

Use this guide for every valid pre-match prediction, lineup-time reanalysis, and post-match diagnostic.

## Internal score audit

1. Load the versioned prediction artifact produced by `scripts/score_model.py predict`. Do not estimate a second pair of scoring rates for exact scores.
2. Use its full regulation-time score grid, including `(0, 0)`, after validating non-negative finite cells, normalization, and reported tail mass. Never discard 0-0 from the distribution or silently renormalize a large omitted tail.
3. Preserve the unconditional exact-score Top 2 as a machine-readable distribution audit. Keep its rank 1 equal to `--predicted-score`.
4. Do not show that independent Top 2 in a pre-match image, normal analytical text, concise output, or copyable initial/lineup block. It may be graded only as a post-match diagnostic and never counts as a primary result or ROI.
5. Do not create or display a total-primary-conditioned score pair. A full-time total may be evaluated from the posterior, but it cannot choose, filter, or reorder user-facing scenarios.
6. Use the `exact_scores` and `score_matrix` fields from the same canonical `prediction.json`. `scripts/exact_score_ranker.py` remains a legacy diagnostic only; it must not create a second strict-forward distribution or a user-facing scenario list.

## User-facing joint scenarios

1. Load the archived, validated match-path posterior that jointly represents half-time and second-half goals and reproduces the canonical full-time score and HT/FT marginals.
2. Aggregate the path cells into genuine `(HT/FT × full-time score)` events and rank them by joint probability. Freeze and validate the global Top 2, then publicly display exactly those two events in descending joint-probability order. Every displayed row contains one inseparable HT/FT outcome, full-time score, and genuine joint probability. If both events share an HT/FT label but have different scores, keep both rows and repeat the label. Never display a third event, complete a branch set, or reorder either event from an independent marginal.
3. Derive displayed 1X2, totals, goal range, and BTTS from the same posterior. Never multiply an HT/FT marginal by an exact-score marginal, put independent Top 2 lists side by side, or replace a score to force terminal-result agreement.
4. The joint artifact must bind the active fixture/version and pass pre-kickoff timing, input-hash, normalization, tail, convergence, and marginal-consistency checks. If it is missing or invalid, show `数据不足` and no scenario rows. Old records, prose, legacy display fields, and manual mappings are not fallbacks.
5. Label every displayed joint event `高方差参考（不计主推）`. The two global Top-2 events and their hit or miss never enter primary accuracy, profit, or ROI.

## Mandatory internal 0-0 calculation

- Calculate the 0-0 probability and its one-based exact-score rank for every pre-match and lineup-check analysis.
- Archive the audit with `--zero-zero-probability` and `--zero-zero-rank`, plus `--zero-zero-odds` and `--zero-zero-ev` only when current exact-score odds were actually collected. Never infer them from 1X2 or totals.
- In normal pre-match output, 0-0 appears only when its genuine joint event belongs to the frozen, validated global Top 2. Show that event's joint probability; do not append the independent 0-0 marginal, rank, odds, or EV.
- Otherwise keep the 0-0 fields machine-only. They must not leak through initial, lineup, or review recommendations, notes, risks, chart captions, hit-rank summaries, or copyable text.

## Archive and review

- Continue archiving the unconditional Top 2 with repeated `--exact-score-pick SCORE:PROBABILITY`, keep `--predicted-score` equal to unconditional rank 1, and preserve the 0-0 parameters above. These remain internal audit fields.
- Legacy `--display-exact-score-pick SCORE:PROBABILITY:UNCONDITIONAL_RANK` and `--display-exact-score-event-probability` values may remain for backward-compatible machine audit, but they no longer define anything displayed to the user.
- Pass `--joint-scenario-file <joint-scenarios.json>` to bind the immutable path posterior and its canonical global Top-2 audit. The archive validator must reproduce the ranked events and reject a wrong fixture/version, event probability, ordering, artifact hash, cutoff, or marginal audit. The versioned display layer exposes exactly those two frozen events, in order, with each event's HT/FT label, score, and joint probability kept together; callers cannot request, suppress, reorder, deduplicate, or hand-pick them.
- On a lineup check, recalculate and archive the internal exact-score audit, internal HT/FT diagnostic, and joint path artifact. Preserve prior versions in `revisions`; never carry a stale scenario forward by hand.
- During review, report only the frozen public joint-event Top 2 in its original order. Keep unconditional exact-score Top-1/Top-2 and HT/FT Top-1/Top-2 selections, hit ranks, and grading machine-only. None alters primary betting statistics or global weights from a small sample.
