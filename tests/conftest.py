"""Shared fixtures: live loopback mock sites and isolated collector runs."""
from __future__ import annotations
import sys
from contextlib import contextmanager
from pathlib import Path
import pytest
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from jobtrace.clock import FixedClock, parse_instant  # noqa: E402
from jobtrace.config import load_config  # noqa: E402
from jobtrace.mock_site import create_app  # noqa: E402
from jobtrace.pipeline import run_collection  # noqa: E402
from jobtrace.serving import start_server  # noqa: E402
FIXED = "2026-01-15T09:00:00Z"
@pytest.fixture
def fixed_clock() -> FixedClock: return FixedClock(parse_instant(FIXED))
@pytest.fixture
def mock_apps():
    def make(scenario: str): return create_app(scenario)
    return make
@pytest.fixture
def live_mock():
    @contextmanager
    def serve(scenario: str, page_size: int | None = None):
        server = start_server(create_app(scenario, page_size=page_size), port=0)
        try:
            yield f"http://127.0.0.1:{server.port}", server
        finally:
            server.stop()
    return serve
@pytest.fixture
def loaded_factory(tmp_path):
    def make(base_url: str, *, fixed_at: str = FIXED, close_after: int = 2):
        return load_config(
            overrides={
                "site.base_url": base_url,
                "site.allowed_hosts": ["127.0.0.1"],
                "site.allowed_ports": [int(base_url.rsplit(":", 1)[1])],
                "storage.database": str(tmp_path / "jobtrace.sqlite3"),
                "storage.raw_dir": str(tmp_path / "raw"),
                "storage.report_dir": str(tmp_path / "reports"),
                "storage.checkpoint": str(tmp_path / "checkpoint.json"),
                "clock.fixed_at": fixed_at,
                "lifecycle.close_after_missing_runs": close_after,
            },
            environ={},
        )
    return make
@pytest.fixture
def collect(live_mock, loaded_factory):
    def run(
        scenario: str,
        *,
        run_id: str,
        fixed_at: str = FIXED,
        close_after: int = 2,
        resume: bool = False,
    ):
        with live_mock(scenario) as (base_url, _server):
            loaded = loaded_factory(base_url, fixed_at=fixed_at, close_after=close_after)
            outcome = run_collection(
                loaded,
                scenario=scenario,
                run_id=run_id,
                resume=resume,
                sleep=lambda _seconds: None,
            )
        return loaded, outcome
    return run
