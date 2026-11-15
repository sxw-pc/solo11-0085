"""Command-line entry point for the offline collector."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable

from . import __version__
from .commands import (
    command_collect,
    command_demo,
    command_lint_size,
    command_report,
    command_serve_mock,
    command_serve_review,
)
from .config import DEFAULT_CONFIG, DEFAULT_SELECTORS
from .errors import JobTraceError
from .mock_site import FIXTURES_DIR


def _env_int(name: str, fallback: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        return int(raw)
    except ValueError as exc:
        raise SystemExit(f"{name} must be an integer, got {raw!r}") from exc


def _storage_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", help="SQLite database path override")
    parser.add_argument("--raw-dir", help="raw evidence directory override")
    parser.add_argument("--report-dir", help="report output directory override")
    parser.add_argument("--checkpoint", help="pagination checkpoint path override")
    parser.add_argument("--base-url", help="loopback mock base URL override")
    parser.add_argument("--clock", help="fixed ISO-8601 clock, or 'system'")


def _config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="YAML config path")
    parser.add_argument("--selectors", default=str(DEFAULT_SELECTORS), help="selector YAML path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobtrace", description="Offline synthetic job metadata lifecycle collector")
    parser.add_argument("--version", action="version", version=f"jobtrace {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)

    demo = commands.add_parser("demo", help="start mock site and review console, run once")
    _config_options(demo)
    _storage_options(demo)
    demo.add_argument("--scenario", default=os.environ.get("JOBTRACE_SCENARIO", "baseline"))
    demo.add_argument("--port", type=int, default=_env_int("JOBTRACE_MOCK_PORT", 8765))
    demo.add_argument("--review-port", type=int, default=_env_int("JOBTRACE_REVIEW_PORT", 8766))
    demo.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    demo.add_argument("--run-id", default=None)
    demo.add_argument("--no-serve", action="store_true", help="exit after one-shot setup")

    collect = commands.add_parser("collect", help="run one incremental collection")
    _config_options(collect)
    _storage_options(collect)
    collect.add_argument("--scenario", default=None)
    collect.add_argument("--with-mock", action="store_true", help="start embedded mock site")
    collect.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    collect.add_argument("--run-id", default=None)
    collect.add_argument("--resume", action="store_true", help="resume from list cursor")
    collect.add_argument("--no-report", action="store_true")

    report = commands.add_parser("report", help="render reports from SQLite")
    _config_options(report)
    report.add_argument("--db", help="SQLite database path override")
    report.add_argument("--out", help="report output directory override")
    report.add_argument("--site", help="site key to report")
    report.add_argument("--clock", help="fixed ISO-8601 clock, or 'system'")

    serve_mock = commands.add_parser("serve-mock", help="serve the synthetic site")
    serve_mock.add_argument("--scenario", default=os.environ.get("JOBTRACE_SCENARIO", "baseline"))
    serve_mock.add_argument("--port", type=int, default=_env_int("JOBTRACE_MOCK_PORT", 8765))
    serve_mock.add_argument("--page-size", type=int, default=None)
    serve_mock.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))

    serve_review = commands.add_parser("serve-review", help="serve the read-only console")
    _config_options(serve_review)
    serve_review.add_argument("--db", help="SQLite database path override")
    serve_review.add_argument("--port", type=int, default=_env_int("JOBTRACE_REVIEW_PORT", 8766))

    lint = commands.add_parser("lint-size", help="check source line budgets")
    lint.add_argument("--root", default=".")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handlers: dict[str, Callable[[argparse.Namespace], int]] = {
        "demo": command_demo, "collect": command_collect, "report": command_report,
        "serve-mock": command_serve_mock, "serve-review": command_serve_review,
        "lint-size": command_lint_size,
    }
    try:
        return handlers[args.command](args)
    except JobTraceError as exc:
        print(f"error[{exc.code}]: {exc.message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        return 130
