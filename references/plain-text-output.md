# Copyable plain-text output

## Commands

Render the archived version appropriate to each workflow:

```bash
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind initial
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review
```

The formatter remains the canonical text audit and fallback. In the default image-plus-text
presentation, show its required information once as the normal accompanying analysis; do not
append a second duplicate under `可复制纯文本版`. Use that heading and append the exact raw
formatter result only when the user explicitly requests a copyable block or text-only mode.
Show user-facing text as ordinary wrapped text, not a fenced code block.

## Required fields

- Initial: match identity and kickoff, primary or explicit no-primary, probability and EV when applicable, non-settled secondary references, half-time and HT/FT observations, exactly two display score scenarios, core rationale, risks, and the analysis disclaimer.
- Lineup check: match identity and check time, explicit maintained/changed/cancelled primary state, current primary or no-primary, non-settled secondary references, half-time and HT/FT observations, exactly two display score scenarios, current rationale, risks, and the analysis disclaimer.
- Review: verified half-time and final scores, final active settlement basis, primary result or `无正式推荐（不结算、不计战绩）`, learning scope, non-settled secondary references, score-scenario diagnostic, causal learning, league primary record, cumulative primary record, and the review disclaimer.

Preserve complete recommendation, risk, and learning text. Redact only a non-Top-2 0-0 diagnostic according to [exact-score.md](exact-score.md). Do not describe an observation or secondary reference as won or lost.

When an image is rendered, follow [image-output.md](image-output.md). A formal primary carries
the archive-derived `★`; an observation may carry `◇`; no other status may use the star. The text must add
probability, EV/evidence, failed-gate, or risk context instead of simply transcribing every
table cell from the image.

## Output boundary

The formatter converts archived data to text only. It has no chat-client integration, account configuration, UI automation, clipboard automation, screenshot verification, or external message delivery. Never claim the text was sent anywhere.
