# Copyable plain-text output

## Commands

Render the archived version appropriate to each workflow:

```bash
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind initial
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review
```

The formatter remains the canonical archived-text audit. It is not a scenario fallback: it
must read the system-selected paired joint events from the active validated match-path
artifact and must never compose them from separate exact-score or HT/FT fields. In the default image-plus-text
presentation, show its required information once as the normal accompanying analysis; do not
append a second duplicate under `可复制纯文本版`. Use that heading and append the exact raw
formatter result only when the user explicitly requests a copyable block or text-only mode.
Show user-facing text as ordinary wrapped text, not a fenced code block.

## Required fields

- Initial: match identity and kickoff, formal primary or explicit no-primary, probability and EV when applicable, non-settled secondary references, complete half-time, full-time 1X2, goal-range and BTTS marginal distributions derived from the validated joint posterior, each field's Top-1/Top-2 gap and `较明确/分歧` status, the one public total-goal range deterministically mapped from the frozen joint Rank 1 score, and exactly the frozen, validated global joint-event Top 2 in descending joint-probability order. For each event, show its inseparable HT/FT label, full-time score, and genuine joint probability; preserve both rows when their HT/FT labels match but their scores differ. Do not repeat Rank 2's goal-range label in normal text. Include structured evidence coverage and the analysis disclaimer. If the joint artifact is unavailable or invalid, show `数据不足` for the public range and paired events with no fallback.
- Lineup check: match identity and check time, explicit maintained/changed/cancelled formal-primary state, current primary or no-primary, non-settled secondary references, the same complete marginal distributions/gaps, the active lineup-check artifact's single Rank-1-score-derived range, and its frozen global joint-event Top 2 with each HT/FT-score pairing intact, structured evidence coverage, and the analysis disclaimer. If validation fails, show `数据不足`; never reuse the initial version's scenarios manually.
- Review: the verified final score and, when available, the verified half-time score; when a half-time score was not required for settlement and could not be verified, show `未取得` and leave the related observation ungraded. Also include the final active settlement basis, primary result or `主推：无正式推荐（不结算、不计战绩）`, learning scope, non-settled secondary references, the frozen public joint Rank-1-derived range and joint-event Top 2 in their original order, causal learning, league primary record, cumulative primary record, and the review disclaimer. Keep independent unconditional exact-score and HT/FT Top-2 selections, hit ranks, and grading machine-only. The same information must support the deterministic review image without rewriting any pre-match direction.

## Primary and observation placement

Derive the card's `主推` cell from the archive; caller prose cannot change it:

1. Show the unique formal primary, with archive-derived `★`.
2. If no formal primary exists, show exactly `无正式主推` in the card and
   `主推：无正式推荐` in initial/lineup text.

A separately archived and diagnostically qualified observation may appear once in accompanying
text or audit with `◇` and an explicit no-bet/non-counting label. It never replaces the card's
no-primary wording. Never derive a headline from the independent 1X2 or goal-range marginal
Top-1, and do not emit an unlabelled standalone `无` placeholder. If the required joint artifact
is absent or invalid, retain the no-primary state and show `数据不足` only for the public range
and paired-event projections.

In initial, lineup, and review output, never print the independent unconditional exact-score
Top 2, legacy total-conditioned score pair, HT/FT Top 2, their hit ranks or grading, or the
hidden 0-0 audit. Those fields remain machine-only and are not summarized publicly. Do not print
free-form `recommendation` or `notes` as a betting direction: those strings cannot bypass
formal-pick or observation gates. Initial/lineup output instead prints a fixed model-scope line
and structured `data_quality`/guardrail evidence coverage. Review keeps its causal learning
text after the existing review-specific sanitization and shows only the frozen public joint
Top 2. Never describe an observation or
secondary reference as won or lost.

When an image is rendered, follow [image-output.md](image-output.md). Initial and lineup images
use the fixed simple eight-column table. Total goals show exactly the range mapped from the
frozen joint Rank 1 score; HT/FT and score show the frozen joint Top 2. Complete half-time, 1X2,
goal-range and BTTS marginal calculations remain in text or audit but cannot fill those compact
slots. A formal primary carries the archive-derived `★`. A separately qualified observation
may carry `◇` only in accompanying text/audit and remains excluded from stake, settlement,
win/loss, profit, and ROI. The image and normal text must reference the same Rank 1 range and
paired joint events. Normal text does not repeat the two internal score-derived range labels.
If either side cannot validate the joint artifact, both use `数据不足` rather than independent
lists, prose inference, or hand-filled values. No image may replace clipped content with an
ellipsis; wrap, reduce the font, or grow the layout.

## Output boundary

The formatter converts archived data to text only. It has no chat-client integration, account configuration, UI automation, clipboard automation, screenshot verification, or external message delivery. Never claim the text was sent anywhere.
