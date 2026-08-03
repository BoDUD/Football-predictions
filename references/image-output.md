# Prediction image and text output

Use this contract for every user-requested daily slate and, by default, for a single-match
prediction. The user-facing result contains one deterministic image plus concise analytical
text. Do not repeat the same content a second time under a `可复制纯文本版` heading unless the
user explicitly asks for a copyable block.

## Image renderer

Build a UTF-8 JSON payload from the final archived pre-match version and bind it to the
workspace prediction archive:

```bash
python scripts/prediction_card_renderer.py \
  --input slate.json \
  --history .codex/soccer-predict/history.json \
  --output slate.png
```

SVG is the dependency-free output. PNG/JPEG output uses Pillow and a real Chinese font; if a
raster dependency or Chinese font is unavailable, render SVG instead of substituting broken
glyphs. Save generated files under the workspace's ignored
`.codex/soccer-predict/rendered/` directory and display the absolute output path as an image.

Each row contains `archive_match_id`, match time, competition, teams, recommendation status,
the strongest direction, goal range, exactly two HT/FT probability shapes, and exactly two
score references. Supply the HT/FT and score fields as two-element JSON arrays, not as
slash-packed free text. Use only pre-kickoff values from the same active version. Never put a
later result, closing price, or reviewed learning into a pre-match image.

The renderer loads the active pending prematch record, verifies the fixture, and derives both
the formal-primary label and its star from `primary_pick`. Callers cannot supply `star` and
cannot turn an unarchived observation into a formal row. A no-primary or observation row must
bind to an archive whose active `primary_pick` is null.

## Marker semantics

- `★` means the row has one unique, archived, policy-enabled formal primary that passed every
  active model, market, data-quality, evidence, and timing gate. The renderer must derive and
  append it immediately after that primary direction. A daily slate may have multiple starred
  rows, but never more than one starred direction per match.
- `◇` means the displayed direction is the strongest structured observation. It may be useful
  for learning or monitoring, but it is not a bet and must never be called a primary.
- `无正式推荐` means no executable direction qualified. Do not attach a star.
- Reject every caller-supplied `star` field, every formal row without an archived active
  primary, and every observation/no-bet row whose archive contains one. The image must not
  provide a visual bypass around the recommendation policy.

The image legend must explain both markers. Do not use a red star merely because a model has
high probability, a historical cohort looks strong, or the user prefers more picks.

## Accompanying text

After the image, provide compact normal text that adds information rather than transcribing
the table. For each starred primary, state probability, current odds, recalculated EV/edge,
market provenance, data quality, and the independent evidence that cleared the gate. For a
diamond observation, state the most important failed gate. Include material lineup, tail-risk,
and data limitations.

Preserve critical settlement wording inside the normal review text. In particular, a reviewed
match with no primary must still contain exactly:

```text
主推：无正式推荐（不结算、不计战绩）
```

The poster changes presentation only. It does not change primary counting, settlement,
profit/ROI, market eligibility, or the requirement for a complete pre-kickoff archive.
The renderer writes a local file only. It has no WeChat sender, chat-client automation,
account configuration, clipboard delivery, or external-message side effect.
