# Copyable plain-text output

## Commands

Render the archived version appropriate to each workflow:

```bash
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind initial
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind lineup-check
python scripts/plain_text_formatter.py --base-dir <workspace> --match-id <id> --kind review
```

Append the exact result under `可复制纯文本版` in the Codex response. Show it as ordinary wrapped text, not a fenced code block.

## Required fields

- Initial: match identity and kickoff, primary or explicit no-primary, probability and EV when applicable, non-settled secondary references, half-time and HT/FT observations, exactly two display score scenarios, core rationale, risks, and the analysis disclaimer.
- Lineup check: match identity and check time, explicit maintained/changed/cancelled primary state, current primary or no-primary, non-settled secondary references, half-time and HT/FT observations, exactly two display score scenarios, current rationale, risks, and the analysis disclaimer.
- Review: verified half-time and final scores, final active settlement basis, primary result or `无正式推荐（不结算、不计战绩）`, learning scope, non-settled secondary references, score-scenario diagnostic, causal learning, league primary record, cumulative primary record, and the review disclaimer.

Preserve complete recommendation, risk, and learning text. Redact only a non-Top-2 0-0 diagnostic according to [exact-score.md](exact-score.md). Do not describe an observation or secondary reference as won or lost.

## Output boundary

The formatter converts archived data to text only. It has no chat-client integration, account configuration, UI automation, clipboard automation, screenshot verification, or external message delivery. Never claim the text was sent anywhere.
