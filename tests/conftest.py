from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

import pytest

CATEGORY_MARKERS = (
    "unit",
    "property",
    "schema_contract",
    "documentation_contract",
    "integration_replay",
    "live_canary",
    "e2e",
    "uncategorized",
)

FILE_CATEGORIES = {
    "test_corner_history_dataset_builder.py": "integration_replay",
    "test_corner_model.py": "unit",
    "test_corner_model_manager.py": "integration_replay",
    "test_corner_ranker.py": "unit",
    "test_domain_settlement.py": "unit",
    "test_lineup_source_contract.py": "documentation_contract",
    "test_exact_score_ranker.py": "unit",
    "test_forward_policy.py": "schema_contract",
    "test_forward_validation.py": "schema_contract",
    "test_history_importer.py": "integration_replay",
    "test_htft_holdout_evaluator.py": "integration_replay",
    "test_htft_model.py": "unit",
    "test_htft_ranker.py": "unit",
    "test_joint_path_kernel.py": "unit",
    "test_joint_scenario_model.py": "unit",
    "test_league_model_manager.py": "integration_replay",
    "test_lineup_scheduler.py": "unit",
    "test_memory_store.py": "integration_replay",
    "test_plain_text_formatter.py": "integration_replay",
    "test_prediction_card_renderer.py": "integration_replay",
    "test_public_market_outlook.py": "unit",
    "test_review_card_renderer.py": "integration_replay",
    "test_review_scheduler.py": "unit",
    "test_release_version.py": "schema_contract",
    "test_score_model.py": "unit",
    "test_soccer_watchdog.py": "unit",
    "test_source_evidence.py": "integration_replay",
    "test_titan_corner_history_collector.py": "integration_replay",
    "test_titan_corner_odds_collector.py": "integration_replay",
    "test_cli_doctor.py": "unit",
}


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Give every collected test one explicit reporting category.

    This is deliberately a reporting taxonomy, not a claim that old unit tests provide
    live-provider or full end-to-end coverage.
    """

    for item in items:
        existing = {
            marker.name
            for marker in item.iter_markers()
            if marker.name in CATEGORY_MARKERS
        }
        if existing:
            continue
        category = FILE_CATEGORIES.get(Path(str(item.path)).name, "uncategorized")
        item.add_marker(getattr(pytest.mark, category))


def pytest_collection_finish(session: pytest.Session) -> None:
    counts: Counter[str] = Counter()
    for item in session.items:
        for category in CATEGORY_MARKERS:
            if item.get_closest_marker(category) is not None:
                counts[category] += 1
                break

    lines = ["### Pytest category inventory", "", "| Category | Tests |", "|---|---:|"]
    lines.extend(
        f"| `{category}` | {counts[category]} |" for category in CATEGORY_MARKERS
    )
    lines.extend(["", f"**Total collected:** {len(session.items)}", ""])

    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(
            "Test categories: "
            + ", ".join(
                f"{category}={counts[category]}" for category in CATEGORY_MARKERS
            )
        )

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
