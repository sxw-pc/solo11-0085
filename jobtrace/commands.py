"""CLI command implementations."""

from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .clock import clock_from_config
from .config import DEFAULT_CONFIG, DEFAULT_SELECTORS, LoadedConfig, load_config
from .errors import ConfigError
from .lint_size import check
from .mock_site import FIXTURES_DIR
from .mock_site import create_app as create_mock_app
from .pipeline import run_collection
from .report import write_reports
from .requirement_evidence import requirement_report
from .review import create_app as create_review_app
from .serving import RunningServer, start_server, wait_forever
from .store import Store


def _port_from_url(url: str) -> int:
    parts = urlsplit(url)
    return parts.port or (443 if parts.scheme == "https" else 80)


def _with_base(overrides: dict[str, Any], base_url: str) -> None:
    overrides["site.base_url"] = base_url.rstrip("/")
    overrides["site.allowed_hosts"] = [urlsplit(base_url).hostname or ""]
    overrides["site.allowed_ports"] = [_port_from_url(base_url)]


def _common_overrides(args: Namespace) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    base_url = getattr(args, "base_url", None)
    if base_url:
        _with_base(overrides, base_url)
    mapping = (
        ("db", "storage.database"), ("raw_dir", "storage.raw_dir"),
        ("report_dir", "storage.report_dir"), ("checkpoint", "storage.checkpoint"),
    )
    for attribute, dotted in mapping:
        value = getattr(args, attribute, None)
        if value:
            overrides[dotted] = value
    fixed = getattr(args, "clock", None)
    if fixed:
        overrides["clock.fixed_at"] = None if fixed.lower() == "system" else fixed
    scenario = getattr(args, "scenario", None)
    if scenario:
        overrides["site.scenario"] = scenario
    return overrides


def _load(args: Namespace, overrides: dict[str, Any] | None = None) -> LoadedConfig:
    values = _common_overrides(args)
    values.update(overrides or {})
    return load_config(
        getattr(args, "config", DEFAULT_CONFIG),
        getattr(args, "selectors", DEFAULT_SELECTORS), overrides=values,
    )


def _mock_server(scenario: str, port: int,
                 fixtures_dir: str | None) -> tuple[RunningServer, str]:
    app = create_mock_app(scenario, fixtures_dir=fixtures_dir or FIXTURES_DIR)
    try:
        server = start_server(app, "127.0.0.1", port)
    except OSError as exc:
        raise ConfigError(f"cannot bind mock site on 127.0.0.1:{port}: {exc}") from exc
    return server, f"http://127.0.0.1:{server.port}"


def _write_reports(loaded: LoadedConfig) -> dict[str, str]:
    clock = clock_from_config(loaded.config.clock.fixed_at)
    store = Store(loaded.config.storage.database, readonly=True)
    try:
        return write_reports(store, loaded.config.storage.report_dir, clock, loaded.config.site.name)
    finally:
        store.close()


def _print_paths(paths: dict[str, str]) -> None:
    print(f"Report: {paths['markdown']}")
    print(f"JSON: {paths['json']}")
    print(f"CSV: {paths['csv']}")
    print(f"JSONL: {paths['jsonl']}")


def command_collect(args: Namespace) -> int:
    preview = _load(args, {})
    scenario = args.scenario or preview.config.site.scenario
    mock: RunningServer | None = None
    base_url = args.base_url
    if args.with_mock:
        base_url = base_url or preview.config.site.base_url
        mock, base_url = _mock_server(scenario, _port_from_url(base_url), args.fixtures_dir)
    try:
        overrides: dict[str, Any] = {}
        if base_url:
            _with_base(overrides, base_url)
        overrides["site.scenario"] = scenario
        loaded = _load(args, overrides)
        outcome = run_collection(
            loaded, scenario=scenario, run_id=args.run_id, resume=args.resume,
        )
        report_paths = None if args.no_report else _write_reports(loaded)
        summary, lifecycle = outcome.summary(), outcome.lifecycle.summary()
        print(f"Run: {outcome.run_id}")
        print(f"Scenario: {scenario}")
        print(f"Status: {summary['stop_reason']} complete={str(summary['complete']).lower()}")
        print(f"Jobs: discovered={summary['unique_jobs']} details_ok={summary['details_ok']} "
              f"failed={summary['details_failed']} duplicates={summary['duplicate_ids']}")
        print("Lifecycle: new={new_jobs} changed={changed_events} closed={closed} "
              "reopened={reopened} disappeared={disappeared}".format(**lifecycle))
        if report_paths:
            print(f"Report: {report_paths['markdown']}")
        return 0
    finally:
        if mock is not None:
            mock.stop()


def command_report(args: Namespace) -> int:
    overrides = {"storage.report_dir": args.out} if args.out else {}
    loaded = _load(args, overrides)
    clock = clock_from_config(loaded.config.clock.fixed_at)
    store = Store(loaded.config.storage.database, readonly=True)
    try:
        paths = write_reports(
            store, loaded.config.storage.report_dir, clock,
            args.site or loaded.config.site.name,
        )
    finally:
        store.close()
    _print_paths(paths)
    return 0


def command_requirements(args: Namespace) -> int:
    """Read-only projection of derived requirement-impact evidence."""
    loaded = _load(args, {})
    database = args.db or loaded.config.storage.database
    if not Path(database).is_file():
        raise ConfigError(f"database not found: {database}")
    store = Store(database, readonly=True)
    try:
        job = None
        if args.job:
            job = store.find_job(args.site or loaded.config.site.name, args.job)
            if job is None:
                raise ConfigError(f"job not found: {args.job}")
        report = requirement_report(store, job_uid=job["job_uid"] if job else None,
                                    rules_version=args.rules_version)
    finally:
        store.close()
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    summary: dict[str, dict[str, int]] = {}
    for item in report["evidence"]:
        counts = summary.setdefault(item["contract"], {})
        counts[item["change_kind"]] = counts.get(item["change_kind"], 0) + 1
    print(f"contracts: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}", file=sys.stderr)
    return 0


def command_demo(args: Namespace) -> int:
    scenario = args.scenario
    mock, base_url = _mock_server(scenario, args.port, args.fixtures_dir)
    review: RunningServer | None = None
    try:
        overrides: dict[str, Any] = {}
        _with_base(overrides, base_url)
        overrides["site.scenario"] = scenario
        loaded = _load(args, overrides)
        outcome = run_collection(loaded, scenario=scenario, run_id=args.run_id, resume=False)
        report_paths = _write_reports(loaded)
        review_app = create_review_app(loaded.config.storage.database)
        try:
            review = start_server(review_app, "127.0.0.1", args.review_port)
        except OSError as exc:
            raise ConfigError(
                f"cannot bind review console on 127.0.0.1:{args.review_port}: {exc}"
            ) from exc
        summary = outcome.summary()
        print(f"Mock site: {base_url}")
        print(f"Review console: http://127.0.0.1:{review.port}")
        print(f"Run: {outcome.run_id} scenario={scenario} complete={str(summary['complete']).lower()}")
        _print_paths(report_paths)
        if args.no_serve:
            return 0
        print("Press Ctrl+C to stop the local servers.", flush=True)
        wait_forever()
        return 0
    finally:
        if review is not None:
            review.stop()
        mock.stop()


def command_serve_mock(args: Namespace) -> int:
    app = create_mock_app(args.scenario, fixtures_dir=args.fixtures_dir or FIXTURES_DIR,
                          page_size=args.page_size)
    print(f"Mock site: http://127.0.0.1:{args.port} (scenario={args.scenario})", flush=True)
    app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
    return 0


def command_serve_review(args: Namespace) -> int:
    loaded = _load(args, {})
    app = create_review_app(args.db or loaded.config.storage.database)
    print(f"Review console: http://127.0.0.1:{args.port}", flush=True)
    app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
    return 0


def command_lint_size(args: Namespace) -> int:
    report = check(Path(args.root).resolve())
    print(f"Checked {len(report.files)} source files; total {report.total} code lines.")
    for name, lines in sorted(report.files.items(), key=lambda item: item[1], reverse=True)[:5]:
        print(f"  {lines:4d}  {name}")
    if report.oversized:
        print("Files over the 300-line limit:")
        for name, lines in sorted(report.oversized.items()):
            print(f"  {lines:4d}  {name}")
        return 1
    if not 2500 <= report.total <= 4500:
        print("Total code line count is outside the required 2500-4500 range.")
        return 1
    return 0
