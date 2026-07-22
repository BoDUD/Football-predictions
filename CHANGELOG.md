# Changelog

## [2.1.0] - 2026-07-22

### Changed
- Added a persistent Japan-time lineup scheduler with a hard T−30 gate, bounded pre-kickoff retries, expiring claim leases, and invocation-time catch-up.
- Added mandatory standalone lineup tasks plus auditable completion and cleanup of match-specific Codex automations.
- Added exactly two diagnostic exact-score candidates and guarded plain-text WeChat delivery support.

## [2.0.0] - 2026-07-21

### Changed
- Adapted the workflow for Codex with visual output, separate T−30 lineup-check tasks, verified post-match reviews, and timezone-safe scheduling.
- Added one machine-readable active primary pick per match plus secondary formal-pick roles.
- Added primary-first accuracy/ROI statistics while retaining all-formal and legacy market statistics.
- Added guarded calibration, legacy primary migration, and tests; global weight changes remain disabled below 20 graded selections per market.

## [1.0.0] - 2026-04-13

### Added
- Initial release
- 5-step quantitative analysis framework
- Automated data scraping from titan007.com
- Asian handicap and over/under prediction models
- Dual output modes (concise / visual)
- Post-match review and accuracy tracking
- ClawHub and GitHub distribution
