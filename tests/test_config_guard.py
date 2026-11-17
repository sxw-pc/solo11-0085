"""Configuration validation and SSRF-style loopback guard tests."""
from __future__ import annotations
from datetime import datetime, timezone
import yaml
import pytest
from jobtrace.clock import FixedClock
from jobtrace.config import DEFAULT_CONFIG, DEFAULT_SELECTORS, load_config, with_fixed_clock
from jobtrace.errors import ConfigError, UnsafeURLError
from jobtrace.http_client import HttpClient
def test_default_config_is_strictly_loopback():
    loaded = load_config(DEFAULT_CONFIG, DEFAULT_SELECTORS, environ={})
    assert loaded.config.site.base_url == "http://127.0.0.1:8765"
    assert loaded.config.site.allowed_hosts == ("127.0.0.1",)
    assert len(loaded.fingerprint) == 64
    assert loaded.selectors.list.item == "li.job-card"
def test_fingerprint_is_stable_and_sensitive_to_clock():
    first = load_config(environ={})
    second = load_config(environ={})
    changed = with_fixed_clock(first, "2026-01-15T09:00:00Z")
    assert first.fingerprint == second.fingerprint
    assert changed.fingerprint != first.fingerprint
@pytest.mark.parametrize("base_url", [
    "http://not-an-ip:8765", "http://[2001:db8::1]:8765",
    "http://127.0.0.1.not-an-ip:8765", "http://user:pass@127.0.0.1:8765",
])
def test_unsafe_base_urls_are_rejected(base_url):
    with pytest.raises(ConfigError):
        load_config(overrides={"site.base_url": base_url}, environ={})
def test_external_allowlist_entry_is_rejected():
    with pytest.raises(ConfigError):
        load_config(overrides={"site.allowed_hosts": ["127.0.0.1", "not-an-ip"]}, environ={})
def test_unknown_key_is_rejected(tmp_path):
    raw = yaml.safe_load(DEFAULT_CONFIG.read_text(encoding="utf-8"))
    raw["crawl"]["surprise"] = True
    tmp = tmp_path / "invalid.yaml"
    tmp.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown keys"): load_config(tmp, environ={})
def test_http_guard_rejects_non_loopback_before_network():
    loaded = load_config(environ={})
    client = HttpClient(loaded.config.crawl, loaded.config.site.allowed_hosts,
                        loaded.config.site.allowed_ports,
                        FixedClock(datetime(2026, 1, 1, tzinfo=timezone.utc)))
    assert client.guard_url("http://127.0.0.1:8765/jobs?page=1").endswith("page=1")
    for url in (
        "http://not-an-ip/jobs",
        "http://[2001:db8::1]:8765/jobs",
        "http://127.0.0.1:9999/jobs",
        "http://127.0.0.1.not-an-ip/jobs",
        "file:///etc/passwd",
        "http://u:p@127.0.0.1:8765/jobs",
    ):
        with pytest.raises(UnsafeURLError): client.guard_url(url)
    client.close()
