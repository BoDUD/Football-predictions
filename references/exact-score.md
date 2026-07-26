# Exact-Score Candidates

Use this guide for every valid pre-match prediction and lineup-time reanalysis.

## Model and ranking

1. Estimate home and away scoring rates from the same calibrated score distribution used for 1X2 and totals. Apply confirmed-lineup effects before the lineup-check output.
2. Enumerate the full regulation-time score grid from home `0..N` and away `0..N`, including `(0, 0)` before any filtering. Normalize the joint probabilities and rank them by model probability descending. Never discard 0-0 as implausible, overly conservative, or unattractive.
3. Output exactly two distinct scores. Break probability ties by agreement with the 1X2 direction, then by proximity to the model expected total.
4. Keep rank 1 equal to `--predicted-score`. Do not choose a second score merely to create variety.
5. Use `scripts/exact_score_ranker.py --home-xg <value> --away-xg <value>` for the standard independent-Poisson distribution. If a calibrated non-Poisson score model is used, apply the same full-grid and 0-0 audit rules to that model rather than mixing distributions.

## Mandatory internal 0-0 calculation

- Calculate the 0-0 probability and its one-based rank for every pre-match and lineup-check analysis.
- If 0-0 ranks first or second, it must occupy that Top-2 position and appear with its model probability. Never skip it in favor of a more exciting score.
- If 0-0 ranks below second, keep the two true Top-2 candidates and retain the 0-0 audit internally; do not add a separate user-facing row by default.
- Show a separate 0-0 diagnostic only when the user explicitly asks for it. In that case, show current decimal odds and EV only when those odds were actually collected; never infer them from totals or 1X2.
- Archive the audit on every run with `--zero-zero-probability` and `--zero-zero-rank`, plus `--zero-zero-odds` and `--zero-zero-ev` when available. `memory_store.py` rejects missing or inconsistent audits.

## Display and archive

- Show rank, score, model probability, and a short scenario label for both candidates in visual and concise modes.
- Do not duplicate 0-0 as a separate audit line when it already appears in the Top-2. When it is outside the Top-2, omit it from normal visual, concise, WeChat, and review output unless the user explicitly requests the diagnostic.
- Do not leak a hidden non-Top-2 audit through recommendation, notes, risks, chart captions, or other prose. Its probability, rank, odds, and EV remain machine-readable archive fields only.
- Label both `高方差参考（不计主推）`. They are not formal bets and never enter primary accuracy/ROI.
- Show exact-score odds and EV only when current market odds were actually collected. Missing odds must remain `数据未取得`; never infer them from 1X2 or totals.
- Archive both with repeated `--exact-score-pick SCORE:PROBABILITY`. On a lineup check, recalculate both and preserve the previous pair in `revisions`.
- During review, report Top-1 and Top-2 hit diagnostics separately. Do not use either diagnostic to change global weights from a small sample.
