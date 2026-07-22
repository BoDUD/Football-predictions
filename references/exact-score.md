# Exact-Score Candidates

Use this guide for every valid pre-match prediction and lineup-time reanalysis.

## Model and ranking

1. Estimate home and away scoring rates from the same calibrated score distribution used for 1X2 and totals. Apply confirmed-lineup effects before the lineup-check output.
2. Enumerate plausible regulation-time score pairs, normalize their joint probabilities, and rank them by model probability descending.
3. Output exactly two distinct scores. Break probability ties by agreement with the 1X2 direction, then by proximity to the model expected total.
4. Keep rank 1 equal to `--predicted-score`. Do not choose a second score merely to create variety.

## Display and archive

- Show rank, score, model probability, and a short scenario label for both candidates in visual and concise modes.
- Label both `高方差参考（不计主推）`. They are not formal bets and never enter `primary` or `all_formal` accuracy/ROI.
- Show exact-score odds and EV only when current market odds were actually collected. Missing odds must remain `数据未取得`; never infer them from 1X2 or totals.
- Archive both with repeated `--exact-score-pick SCORE:PROBABILITY`. On a lineup check, recalculate both and preserve the previous pair in `revisions`.
- During review, report Top-1 and Top-2 hit diagnostics separately. Do not use either diagnostic to change global weights from a small sample.
