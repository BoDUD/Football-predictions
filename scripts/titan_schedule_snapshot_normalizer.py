#!/usr/bin/env python3
"""Offline, atomic normalization for frozen Titan schedule snapshots."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

try:  # Imported from the repository root.
    from scripts import titan_corner_history_collector as collector
except ImportError:  # Invoked directly as scripts/titan_schedule_snapshot_normalizer.py.
    import titan_corner_history_collector as collector  # type: ignore[no-redef]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Add source_timezone, kickoff_utc, kickoff_epoch, and normalized "
            "competition regimes to existing Titan schedule JSON without network access"
        )
    )
    parser.add_argument(
        "--schedule",
        action="append",
        required=True,
        type=Path,
        help="frozen schedules.json; repeat for multiple snapshots",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--in-place",
        action="store_true",
        help="atomically replace each changed snapshot after validation",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate and report the planned hashes without writing",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    paths = [path.resolve() for path in args.schedule]
    if len(set(paths)) != len(paths):
        raise SystemExit("error: duplicate --schedule path")
    try:
        # Validate every file before writing any of them.  This prevents a bad
        # later input from leaving an earlier snapshot partially upgraded.
        checks = [
            collector.normalize_schedule_snapshot(path, write=False)
            for path in paths
        ]
        reports = (
            [collector.normalize_schedule_snapshot(path, write=True) for path in paths]
            if args.in_place
            else checks
        )
    except collector.CornerCollectionError as error:
        raise SystemExit(f"error: {error}") from error
    print(
        json.dumps(
            {
                "event": (
                    "titan-schedule-snapshots-normalized"
                    if args.in_place
                    else "titan-schedule-snapshots-checked"
                ),
                "normalizer_version": collector.SCHEDULE_NORMALIZER_VERSION,
                "files": len(reports),
                "matches": sum(report["matches"] for report in reports),
                "changed_rows": sum(report["changed_rows"] for report in reports),
                "written_files": sum(bool(report["written"]) for report in reports),
                "reports": reports,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
