# Lineup source fallback

Use this contract during every lineup-check when Titan does not show both starting XIs. Titan remains authoritative for the tracked fixture state and its odds pages; lineup evidence may come from another fixture-bound source.

## Source order

1. Check the official competition organizer or national federation match centre. Prefer a page that publishes one match sheet containing both teams, such as an official UEFA match `lineups` page.
2. Check official club websites, apps, or verified club announcements. Two one-team announcements are required to cover both XIs.
3. Check a public ESPN `soccer/lineups` page as the first general fallback. Require a stable numeric `gameId`, both XIs, and matching fixture metadata.
4. Check Sofascore as the second general fallback. Require an exact event ID, both XIs in the match `Lineups` view, and no projected/probable qualifier. Reject reusable team-pair slugs that are not bound to the exact event.
5. Do not automate FotMob, Flashscore, or Soccerway. Their published terms restrict automated or systematic retrieval. A user-supplied page or screenshot from one of them may be noted as non-official context, but it never upgrades the confirmation state by itself.

Use only normal visible page navigation. Do not bypass a blocked or signed-out page by scraping cookies or storage, and do not call undocumented/private APIs. Move to the next permitted public source and preserve the failed-source reason.

Use search results only to locate the exact page; a search snippet is never lineup evidence. Many competitions submit or publish official match sheets around T-60 to T-75, so refresh the official page during T-30 even when Titan is empty. If a later official pre-kickoff update reports a warm-up replacement, preserve the earlier conflict and let only the newest fixture-bound official source supersede it.

## Bind every source to the fixture

Before using an external lineup, require all of the following:

- both normalized team identities match the Titan fixture;
- the competition or tie matches;
- the converted kickoff represents the same instant, allowing at most 15 minutes for a documented provider rounding difference;
- the home/away orientation matches, or both pages explicitly identify a neutral venue;
- the external page is still pre-match at collection time; and
- the lineup was collected before kickoff with a timezone-aware timestamp.

Reject search snippets, team squad pages, a different leg of a tie, youth/women/reserve teams, and pages bound only by one club name.

## Classify the evidence

Set `lineup_confirmed=true` only when exactly 11 starters are present for each team and one of these bases holds:

- one official organizer/federation match sheet publishes both XIs; or
- two official club announcements publish their respective XIs.

Classify every other case without upgrading it:

- `corroborated`: ESPN and Sofascore both show the same 11+11 for the exact event, but no official source is available;
- `reported`: one general provider shows an actual lineup but there is no second matching source;
- `predicted`: any page says predicted, probable, possible, expected, projected, or uses equivalent wording;
- `conflicting`: sources disagree on any starter or bind to different fixture metadata;
- `unavailable`: no source supplies a complete 11+11.

`corroborated`, `reported`, `predicted`, `conflicting`, and `unavailable` all mean `lineup_confirmed=false`. Cross-provider agreement is useful descriptive evidence but does not become an official match sheet, and matching predictions from multiple sites never become confirmed lineups.

## Capture and use

For every source checked, preserve the URL, stable event/game ID, source tier, visible lineup-status wording, timezone-aware collection time, visible publication/update time when available, matched fixture fields, both XI counts, and any disagreement. In the archive notes and user-visible lineup-check result, name the sources checked and the final classification.

Use confirmed lineups only after mapping player identities safely to the model inputs. If player-role, goalkeeper, formation, injury, or suspension effects cannot be quantified by the current versioned model, describe the evidence without hand-adjusting probabilities.

When confirmation remains unavailable:

- continue the same one-time lineup-check instead of creating another automatic attempt;
- keep `--lineup-confirmed` absent;
- downgrade any lineup-dependent data-quality or recommendation gate;
- do not change a primary on the strength of predicted or single-source reported lineups; and
- do not call scheduler `release` solely for missing lineups when the remaining collection and no-lineup archive can complete safely.
