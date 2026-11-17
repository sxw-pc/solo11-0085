"""Re-validation of a saved cursor prefix before a resumed list crawl.

A saved ``cursor.next_url`` is not by itself proof of anything. On resume the
stored prefix is replayed into the evidence ledger, then every stored page URL is
fetched again and compared byte-for-byte. Any changed bytes, page index, or
snapshot id is preserved as a ledger conflict and the caller starts a new segment
instead of continuing the old chain.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .http_client import HttpClient, HttpResponse
from .page_epoch import EvidenceLedger, PageObservation
from .pagination import Cursor, normalized_page_url
from .parser import page_observation, parse_list_page
from .selectors import Selectors


def revalidate_prefix(client: HttpClient, ledger: EvidenceLedger, cursor: Cursor,
                      selectors: Selectors, page_param: str,
                      archive: Callable[[HttpResponse], Any], first_url: str
                      ) -> tuple[bool, str]:
    """Return ``(prefix_ok, resume_url)``; divergence restarts at ``first_url``."""

    try:
        stored = [PageObservation.from_cursor(item) for item in cursor.pages]
    except (KeyError, TypeError, ValueError):
        return False, first_url
    for page in stored:
        ledger.observe(page)
    conflicts_before = len(ledger.witnesses)
    for page in stored:
        response = client.get(page.url)
        archive(response)
        parsed = parse_list_page(response.text, response.url, selectors)
        ledger.observe(page_observation(response, page.url, parsed))
        declared = parsed.declaration
        if declared.snapshot_id != page.snapshot_id \
                or declared.page_index != page.page_index:
            return False, first_url
    if len(ledger.witnesses) != conflicts_before:
        return False, first_url
    last = stored[-1] if stored else None
    if last is not None and not _same(last.next_url, cursor.next_url):
        return False, first_url
    if last is not None and cursor.snapshot_id and last.snapshot_id != cursor.snapshot_id:
        return False, first_url
    return True, cursor.next_url or first_url


def _same(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return left == right
    try:
        return normalized_page_url(left) == normalized_page_url(right)
    except ValueError:
        return left == right
