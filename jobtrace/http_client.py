"""Guarded loopback HTTP client with bounded retries and response sizes."""
from __future__ import annotations
import ipaddress
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable
from urllib.parse import urlsplit, urlunsplit
import requests
from .clock import Clock, iso_z
from .config import CrawlConfig
from .errors import FetchError, UnsafeURLError
from .fingerprint import sha256_bytes
RETRYABLE_STATUSES = {408, 425, 429, 500, 502, 503, 504}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_CHARSET = re.compile(r"charset=([\w.-]+)", re.IGNORECASE)
@dataclass
class RequestStats:
    logical_requests: int = 0
    attempts: int = 0
    retries: int = 0
    failures: int = 0
    bytes_downloaded: int = 0
    by_status: dict[int, int] = field(default_factory=dict)
    def record_status(self, status: int) -> None:
        self.by_status[status] = self.by_status.get(status, 0) + 1
    def as_dict(self) -> dict[str, object]:
        return {
            "logical_requests": self.logical_requests, "attempts": self.attempts,
            "retries": self.retries, "failures": self.failures,
            "bytes_downloaded": self.bytes_downloaded,
            "by_status": {str(key): self.by_status[key] for key in sorted(self.by_status)},
        }
@dataclass(frozen=True)
class HttpResponse:
    url: str
    status: int
    content: bytes
    text: str
    content_type: str
    sha256: str
    attempts: int
    fetched_at: str
    @property
    def size_bytes(self) -> int: return len(self.content)
class HttpClient:
    def __init__(self, crawl: CrawlConfig, allowed_hosts: Iterable[str],
                 allowed_ports: Iterable[int], clock: Clock, *,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.crawl = crawl
        self.allowed_hosts = {host.lower() for host in allowed_hosts}
        self.allowed_ports = set(allowed_ports)
        self.clock = clock
        self.stats = RequestStats()
        self._sleep = sleep
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {"User-Agent": crawl.user_agent, "Accept": "text/html,application/xhtml+xml"}
        )
    def close(self) -> None: self.session.close()
    def guard_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise UnsafeURLError(f"scheme is not allowed: {parts.scheme or '(none)'}")
        if not parts.hostname: raise UnsafeURLError("URL has no host")
        if parts.username or parts.password or "@" in parts.netloc:
            raise UnsafeURLError("credentials in URLs are forbidden")
        host = parts.hostname.lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError as exc:
            raise UnsafeURLError(
                f"host must be a literal IP address without DNS resolution: {host}"
            ) from exc
        if not address.is_loopback:
            raise UnsafeURLError(f"only loopback addresses are allowed: {host}")
        if host not in self.allowed_hosts:
            raise UnsafeURLError(f"host is not in the allowlist: {host}")
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError as exc:
            raise UnsafeURLError("URL has an invalid port") from exc
        if port not in self.allowed_ports:
            raise UnsafeURLError(f"port is not in the allowlist: {port}")
        return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", parts.query, ""))
    def _too_large(self, url: str, status: int, attempts: int) -> FetchError:
        return FetchError("response exceeds configured size limit", url=url,
                          status=status, attempts=attempts)
    def _read_body(self, response: requests.Response, url: str, attempts: int) -> bytes:
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > self.crawl.max_response_bytes:
            response.close()
            raise self._too_large(url, response.status_code, attempts)
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_content(chunk_size=8192):
                if not chunk: continue
                total += len(chunk)
                if total > self.crawl.max_response_bytes:
                    response.close()
                    raise self._too_large(url, response.status_code, attempts)
                chunks.append(chunk)
        except requests.RequestException as exc:
            raise FetchError(f"failed while reading response: {exc}", url=url,
                             status=response.status_code, retryable=True,
                             attempts=attempts) from exc
        finally:
            response.close()
        self.stats.bytes_downloaded += total
        return b"".join(chunks)
    @staticmethod
    def _decode(content: bytes, content_type: str) -> str:
        match = _CHARSET.search(content_type)
        encoding = match.group(1) if match else "utf-8"
        try:
            return content.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            return content.decode("utf-8", errors="replace")
    def _wait_before_retry(self, attempt: int) -> None:
        self.stats.retries += 1
        self._sleep(self.crawl.retry_backoff_seconds * (2 ** (attempt - 1)))
    def get(self, url: str) -> HttpResponse:
        safe_url = self.guard_url(url)
        self.stats.logical_requests += 1
        last_error = "request failed"
        total_attempts = self.crawl.max_retries + 1
        for attempt in range(1, total_attempts + 1):
            self.stats.attempts += 1
            try:
                response = self.session.get(
                    safe_url,
                    timeout=(self.crawl.connect_timeout_seconds, self.crawl.read_timeout_seconds),
                    allow_redirects=False, stream=True,
                )
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt < total_attempts:
                    self._wait_before_retry(attempt)
                    continue
                self.stats.failures += 1
                raise FetchError(f"request failed after {attempt} attempt(s): {last_error}",
                                 url=safe_url, retryable=True, attempts=attempt) from exc
            self.stats.record_status(response.status_code)
            if response.status_code in REDIRECT_STATUSES:
                response.close()
                self.stats.failures += 1
                raise FetchError("redirects are not followed by the offline collector",
                                 url=safe_url, status=response.status_code, attempts=attempt)
            if response.status_code in RETRYABLE_STATUSES:
                response.close()
                last_error = f"HTTP {response.status_code}"
                if attempt < total_attempts:
                    self._wait_before_retry(attempt)
                    continue
                self.stats.failures += 1
                raise FetchError(
                    f"transient HTTP failure after {attempt} attempt(s): {response.status_code}",
                    url=safe_url, status=response.status_code, retryable=True, attempts=attempt,
                )
            if response.status_code >= 400:
                response.close()
                self.stats.failures += 1
                raise FetchError(f"HTTP {response.status_code} for {safe_url}", url=safe_url,
                                 status=response.status_code, attempts=attempt)
            content_type = response.headers.get("Content-Type", "")
            try:
                content = self._read_body(response, safe_url, attempt)
            except FetchError:
                self.stats.failures += 1
                raise
            if self.crawl.request_interval_seconds:
                self._sleep(self.crawl.request_interval_seconds)
            return HttpResponse(
                url=safe_url, status=response.status_code, content=content,
                text=self._decode(content, content_type), content_type=content_type,
                sha256=sha256_bytes(content), attempts=attempt,
                fetched_at=iso_z(self.clock.now()),
            )
        raise FetchError(last_error, url=safe_url)  # pragma: no cover - loop returns/raises
