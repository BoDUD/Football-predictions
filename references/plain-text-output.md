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

- Initial: match identity and kickoff, primary or explicit no-primary, probability and EV when applicable, non-settled secondary references, complete half-time, full-time 1X2, goal-range and BTTS distributions derived from the validated joint posterior, each field's Top-1/Top-2 gap and `较明确/分歧` status, and the paired joint path events with HT/FT, full-time score, and joint probability. Normally output two paired events; output three only when the versioned complexity rule selects a divided top cluster. Include structured evidence coverage and the analysis disclaimer. If the joint artifact is unavailable or invalid, show `数据不足` for every descriptive field and no scenario fallback.
- Lineup check: match identity and check time, explicit maintained/changed/cancelled primary state, current primary or no-primary, non-settled secondary references, the same complete distributions/gaps, the system-selected two or three paired joint events from the active lineup-check artifact, structured evidence coverage, and the analysis disclaimer. If validation fails, show `数据不足`; never reuse the initial version's scenarios manually.
- Review: verified half-time and final scores, final active settlement basis, primary result or `主推：无正式推荐（不结算、不计战绩）`, learning scope, non-settled secondary references, the archived paired joint-event diagnostic in its original order, internal unconditional exact-score Top-1/Top-2 and HT/FT Top-1/Top-2 diagnostics when gradable, causal learning, league primary record, cumulative primary record, and the review disclaimer. The same information must support the deterministic review image without rewriting any pre-match direction.

In initial and lineup output, never print the independent unconditional exact-score Top 2,
legacy total-conditioned score pair, HT/FT Top 2, or hidden 0-0 audit. Those fields remain
machine-readable and may be summarized only in the post-match diagnostic. Do not print
free-form `recommendation` or `notes` as a betting direction: those strings cannot bypass
formal-pick or observation gates. Initial/lineup output instead prints a fixed model-scope line
and structured `data_quality`/guardrail evidence coverage. Review keeps its causal learning
text after the existing review-specific sanitization. Never describe an observation or
secondary reference as won or lost.

When an image is rendered, follow [image-output.md](image-output.md). Initial and lineup images
use the fixed simple eight-column table; total goals show only the highest-probability range
and its lead, while full half-time, 1X2, goal-range and BTTS calculations remain in this text or
the audit. A formal primary carries the archive-derived `★`; an observation may carry `◇`; no
other status may use the star. The image and normal text must reference the same paired joint
events and probabilities. The text adds probability, EV/evidence, failed-gate, or risk context
instead of transcribing every table cell. If either side cannot validate the joint artifact,
both use `数据不足` rather than independent lists, prose inference, or hand-filled values. No
image may replace clipped content with an ellipsis; wrap, reduce the font, or grow the layout.

## Output boundary

The formatter converts archived data to text only. It has no chat-client integration, account configuration, UI automation, clipboard automation, screenshot verification, or external message delivery. Never claim the text was sent anywhere.
