"""Public command-line entry point.

Keep parser construction importable so future domain commands can extend the CLI without
running argument parsing at import time.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__
from .doctor import add_doctor_parser, run_doctor_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="soccer-predict",
        description="Local diagnostics and tooling for the soccer-predict skill.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    doctor_parser = add_doctor_parser(subparsers)
    doctor_parser.set_defaults(handler=run_doctor_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:  # pragma: no cover - argparse requires a subcommand.
        parser.error("a command is required")
    return int(handler(args))
