"""YAML configuration loading, strict validation, and loopback guardrails."""

from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

from .clock import parse_instant
from .errors import ConfigError
from .fingerprint import config_fingerprint
from .selectors import Selectors, load_selectors
from .yamlio import integer, number, port_list, read_yaml, strict_map, text, text_list

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = PROJECT_ROOT / "configs" / "default.yaml"
DEFAULT_SELECTORS = PROJECT_ROOT / "configs" / "selectors.yaml"


@dataclass(frozen=True)
class SiteConfig:
    name: str
    base_url: str
    list_path: str
    page_param: str
    page_size: int
    max_pages: int
    max_details: int
    allowed_hosts: tuple[str, ...]
    allowed_ports: tuple[int, ...]
    scenario: str


@dataclass(frozen=True)
class CrawlConfig:
    connect_timeout_seconds: float
    read_timeout_seconds: float
    max_retries: int
    retry_backoff_seconds: float
    max_response_bytes: int
    request_interval_seconds: float
    user_agent: str


@dataclass(frozen=True)
class StorageConfig:
    database: str
    raw_dir: str
    report_dir: str
    checkpoint: str


@dataclass(frozen=True)
class LifecycleConfig:
    close_after_missing_runs: int


@dataclass(frozen=True)
class ClockConfig:
    fixed_at: str | None


@dataclass(frozen=True)
class AppConfig:
    site: SiteConfig
    crawl: CrawlConfig
    storage: StorageConfig
    lifecycle: LifecycleConfig
    clock: ClockConfig


@dataclass(frozen=True)
class LoadedConfig:
    config: AppConfig
    selectors: Selectors
    fingerprint: str
    config_path: str
    selectors_path: str


def _loopback_host(host: str, context: str) -> None:
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError(f"{context} must be a literal loopback IP, got {host!r}") from exc
    if not address.is_loopback:
        raise ConfigError(f"{context} is not a loopback address: {host}")


def _validate_base_url(base_url: str, hosts: tuple[str, ...], ports: tuple[int, ...]) -> None:
    parts = urlsplit(base_url)
    if parts.scheme not in {"http", "https"}:
        raise ConfigError("site.base_url must use http or https")
    if parts.username or parts.password:
        raise ConfigError("site.base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ConfigError("site.base_url must not contain a query or fragment")
    if parts.path not in {"", "/"}:
        raise ConfigError("site.base_url must not contain a path; use site.list_path")
    host = parts.hostname or ""
    _loopback_host(host, "site.base_url host")
    if host not in hosts:
        raise ConfigError(f"site.base_url host {host!r} is not in site.allowed_hosts")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if port not in ports:
        raise ConfigError(f"site.base_url port {port} is not in site.allowed_ports")


def _parse_site(raw: Any) -> SiteConfig:
    keys = {
        "name", "base_url", "list_path", "page_param", "page_size", "max_pages",
        "max_details", "allowed_hosts", "allowed_ports", "scenario",
    }
    data = strict_map(raw, keys, "site")
    hosts = text_list(data["allowed_hosts"], "site.allowed_hosts")
    for host in hosts:
        _loopback_host(host, "site.allowed_hosts")
    ports = port_list(data["allowed_ports"], "site.allowed_ports")
    base_url = text(data["base_url"], "site.base_url").rstrip("/")
    _validate_base_url(base_url, hosts, ports)
    list_path = text(data["list_path"], "site.list_path")
    if not list_path.startswith("/"):
        raise ConfigError("site.list_path must start with /")
    return SiteConfig(
        name=text(data["name"], "site.name"), base_url=base_url, list_path=list_path,
        page_param=text(data["page_param"], "site.page_param"),
        page_size=integer(data["page_size"], "site.page_size", 1, 100),
        max_pages=integer(data["max_pages"], "site.max_pages", 1, 1000),
        max_details=integer(data["max_details"], "site.max_details", 1, 10000),
        allowed_hosts=hosts, allowed_ports=ports,
        scenario=text(data["scenario"], "site.scenario"),
    )


def _parse_crawl(raw: Any) -> CrawlConfig:
    keys = {
        "connect_timeout_seconds", "read_timeout_seconds", "max_retries",
        "retry_backoff_seconds", "max_response_bytes", "request_interval_seconds",
        "user_agent",
    }
    data = strict_map(raw, keys, "crawl")
    return CrawlConfig(
        connect_timeout_seconds=number(
            data["connect_timeout_seconds"], "crawl.connect_timeout_seconds", 0.05, 60),
        read_timeout_seconds=number(
            data["read_timeout_seconds"], "crawl.read_timeout_seconds", 0.05, 120),
        max_retries=integer(data["max_retries"], "crawl.max_retries", 0, 10),
        retry_backoff_seconds=number(
            data["retry_backoff_seconds"], "crawl.retry_backoff_seconds", 0, 10),
        max_response_bytes=integer(
            data["max_response_bytes"], "crawl.max_response_bytes", 1024, 50_000_000),
        request_interval_seconds=number(
            data["request_interval_seconds"], "crawl.request_interval_seconds", 0, 10),
        user_agent=text(data["user_agent"], "crawl.user_agent"),
    )


def _resolve_path(value: Any, context: str, config_path: Path) -> str:
    raw = Path(text(value, context)).expanduser()
    if not raw.is_absolute():
        base = config_path.resolve().parent
        base = base.parent if base.name == "configs" else base
        raw = base / raw
    return str(raw.resolve())


def _parse_storage(raw: Any, config_path: Path) -> StorageConfig:
    data = strict_map(raw, {"database", "raw_dir", "report_dir", "checkpoint"}, "storage")
    return StorageConfig(
        database=_resolve_path(data["database"], "storage.database", config_path),
        raw_dir=_resolve_path(data["raw_dir"], "storage.raw_dir", config_path),
        report_dir=_resolve_path(data["report_dir"], "storage.report_dir", config_path),
        checkpoint=_resolve_path(data["checkpoint"], "storage.checkpoint", config_path),
    )


def _parse_lifecycle(raw: Any) -> LifecycleConfig:
    data = strict_map(raw, {"close_after_missing_runs"}, "lifecycle")
    return LifecycleConfig(close_after_missing_runs=integer(
        data["close_after_missing_runs"], "lifecycle.close_after_missing_runs", 1, 365
    ))


def _parse_clock(raw: Any) -> ClockConfig:
    data = strict_map(raw, {"fixed_at"}, "clock")
    fixed = data["fixed_at"]
    if fixed is not None:
        fixed = text(fixed, "clock.fixed_at")
        parse_instant(fixed)
    return ClockConfig(fixed_at=fixed)


def _apply_overrides(raw: dict[str, Any], overrides: Mapping[str, Any]) -> None:
    for dotted, value in overrides.items():
        if not isinstance(dotted, str) or "." not in dotted:
            raise ConfigError(f"override key must be dotted, got {dotted!r}")
        node = raw
        parts = dotted.split(".")
        for part in parts[:-1]:
            if not isinstance(node.get(part), dict):
                raise ConfigError(f"cannot override missing mapping: {dotted}")
            node = node[part]
        node[parts[-1]] = value


def _apply_environment(raw: dict[str, Any], environ: Mapping[str, str]) -> None:
    mapping = {
        "JOBTRACE_BASE_URL": "site.base_url", "JOBTRACE_SCENARIO": "site.scenario",
        "JOBTRACE_DB": "storage.database", "JOBTRACE_RAW_DIR": "storage.raw_dir",
        "JOBTRACE_REPORT_DIR": "storage.report_dir",
        "JOBTRACE_CHECKPOINT": "storage.checkpoint", "JOBTRACE_CLOCK": "clock.fixed_at",
    }
    overrides = {dotted: (None if key == "JOBTRACE_CLOCK" and environ[key].lower() == "system"
                          else environ[key]) for key, dotted in mapping.items() if environ.get(key)}
    if overrides:
        _apply_overrides(raw, overrides)


def load_config(config_path: str | Path = DEFAULT_CONFIG,
                selectors_path: str | Path = DEFAULT_SELECTORS, *,
                overrides: Mapping[str, Any] | None = None,
                environ: Mapping[str, str] | None = None) -> LoadedConfig:
    path = Path(config_path)
    raw = read_yaml(path)
    _apply_environment(raw, os.environ if environ is None else environ)
    if overrides:
        _apply_overrides(raw, overrides)
    strict_map(raw, {"site", "crawl", "storage", "lifecycle", "clock"}, "root")
    config = AppConfig(
        site=_parse_site(raw["site"]), crawl=_parse_crawl(raw["crawl"]),
        storage=_parse_storage(raw["storage"], path),
        lifecycle=_parse_lifecycle(raw["lifecycle"]), clock=_parse_clock(raw["clock"]),
    )
    selector_file = Path(selectors_path)
    selectors = load_selectors(selector_file)
    return LoadedConfig(
        config=config, selectors=selectors,
        fingerprint=config_fingerprint(config, selectors),
        config_path=str(path.resolve()), selectors_path=str(selector_file.resolve()),
    )


def with_fixed_clock(loaded: LoadedConfig, fixed_at: str | None) -> LoadedConfig:
    config = replace(loaded.config, clock=ClockConfig(fixed_at=fixed_at))
    return replace(loaded, config=config, fingerprint=config_fingerprint(config, loaded.selectors))
