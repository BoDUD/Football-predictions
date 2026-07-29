# Exact-Score Candidates

Use this guide for every valid pre-match prediction and lineup-time reanalysis.

## Model and ranking

1. Estimate home and away scoring rates from the same calibrated score distribution used for 1X2 and totals. Apply confirmed-lineup effects before the lineup-check output.
2. Enumerate the full regulation-time score grid from home `0..N` and away `0..N`, including `(0, 0)` before any filtering. Normalize the joint probabilities and rank them by model probability descending. Never discard 0-0 from the underlying distribution.
3. Preserve the unconditional Top 2 as the machine-readable distribution audit. Keep its rank 1 equal to `--predicted-score`.
4. When the unique formal primary is a full-time total, build a separate user-facing pair from the primary's net-profit branch: total goals strictly above the line for `over`, or strictly below the line for `under`. Rank those cells by their original unconditional probabilities. Integer-line pushes are not supporting scenarios.
5. For a primary-conditioned pair, show both the unconditional full-match probability and the conditional share within the primary event. Label it `主推成立时的波胆情境（高方差参考）`; never describe the conditional share as the unconditional probability or claim that the pair is the global Top 2.
6. Without a formal total primary, display the unconditional Top 2. Do not create a second score merely for variety.
7. Use `scripts/exact_score_ranker.py --home-xg <value> --away-xg <value>`. Add `--display-total-side <over|under> --display-total-line <line>` only for a formal total primary. If a calibrated non-Poisson model is used, apply the same full-grid, conditional-display, and 0-0 audit rules to that model rather than mixing distributions.

## Mandatory internal 0-0 calculation

- Calculate the 0-0 probability and its one-based rank for every pre-match and lineup-check analysis.
- Preserve 0-0's probability and unconditional one-based rank before any display conditioning.
- In an unconditional display, let 0-0 appear naturally when it ranks first or second.
- In a primary-conditioned total display, show 0-0 only when it belongs to that net-profit branch and ranks in the displayed pair. Otherwise retain it internally even if it belongs to the unconditional Top 2.
- Show a separate 0-0 diagnostic only when the user explicitly asks for it. In that case, show current decimal odds and EV only when those odds were actually collected; never infer them from totals or 1X2.
- Archive the audit on every run with `--zero-zero-probability` and `--zero-zero-rank`, plus `--zero-zero-odds` and `--zero-zero-ev` when available. `memory_store.py` rejects missing or inconsistent audits.

## Display and archive

- Show display rank, score, unconditional model probability, and a short scenario label for both user-facing candidates. For a primary-conditioned pair, also show its conditional share and the condition.
- Do not duplicate 0-0 as a separate audit line when it already appears in the Top-2. When it is outside the Top-2, omit it from normal visual, concise, WeChat, and review output unless the user explicitly requests the diagnostic.
- Do not leak a hidden non-Top-2 audit through recommendation, notes, risks, chart captions, or other prose. Its probability, rank, odds, and EV remain machine-readable archive fields only.
- Label both `高方差参考（不计主推）`. They are not formal bets and never enter primary accuracy/ROI.
- Show exact-score odds and EV only when current market odds were actually collected. Missing odds must remain `数据未取得`; never infer them from 1X2 or totals.
- Archive the unconditional Top 2 with repeated `--exact-score-pick SCORE:PROBABILITY`. For a formal total primary, also archive the displayed pair with repeated `--display-exact-score-pick SCORE:PROBABILITY:UNCONDITIONAL_RANK` plus `--display-exact-score-event-probability`.
- On a lineup check, recalculate both layers and preserve both previous pairs in `revisions`.
- During review, report displayed-pair hits separately from unconditional Top-1/Top-2 diagnostics. Do not use either diagnostic to change global weights from a small sample.
