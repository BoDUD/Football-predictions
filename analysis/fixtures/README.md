# Notebook CI fixtures

`htft_history_validation_smoke.json` is a deliberately tiny, synthetic structure fixture.
It exists only to execute every cell of `analysis/htft_history_validation.ipynb` in clean
CI, where private `.codex/soccer-predict` artifacts are unavailable.

The fixture contains no historical claims and is marked `evidence_eligible: false`. Its
zero-valued metrics, hashes and deployment status must never be used as model validation,
promotion evidence, performance reporting, or a prediction input. A real analyst run keeps
the notebook's default `artifact` mode and validates the workspace's hash-bound artifacts.

`visible_market_snapshot.json` is also synthetic. It exercises the replayable main-market
source contract, HTTP/request metadata normalization, complete-firm parsing, and candidate
price binding; it is not a captured historical Titan market and cannot be used as training or
forward-performance evidence.
