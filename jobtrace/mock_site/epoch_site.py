"""Controllable synthetic list site for page-epoch evidence tests.

Every page is served from an explicit :class:`SnapshotPage` spec, so tests can
declare the snapshot id, its own page index, the total page count, the cards,
and the exact ``next_url``/``prev_url`` relations. The control endpoints switch
the active snapshot between requests, pin a page to a snapshot, or change one
page's content in place, which is enough to reproduce S1->S2 switches, back
links, gaps, total-count conflicts, and same-URL byte changes.
"""

from __future__ import annotations

import html
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, redirect, request

from .data import FIXTURES_DIR


@dataclass(frozen=True)
class SnapshotPage:
    snapshot_id: str
    page: int
    total_pages: int
    cards: tuple[dict[str, Any], ...] = ()
    next_url: str | None = None
    prev_url: str | None = None
    url_page: int | None = None
    marker: str = ""


@dataclass
class EpochSite:
    """Mutable snapshot registry with one-shot per-page overrides."""

    _snapshots: dict[str, dict[int, SnapshotPage]] = field(default_factory=dict)
    _active: str | None = None
    _pins: dict[int, str] = field(default_factory=dict)
    _switch_after: dict[int, str] = field(default_factory=dict)
    _one_shot: dict[int, str] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def register(self, snapshot_id: str, pages: list[SnapshotPage]) -> None:
        with self._lock:
            self._snapshots[snapshot_id] = {
                page.url_page or page.page: page for page in pages
            }
            self._active = self._active or snapshot_id

    @property
    def active(self) -> str | None:
        with self._lock:
            return self._active

    def activate(self, snapshot_id: str) -> None:
        with self._lock:
            if snapshot_id not in self._snapshots:
                raise KeyError(f"unknown snapshot: {snapshot_id}")
            self._active = snapshot_id

    def pin(self, page: int, snapshot_id: str) -> None:
        with self._lock:
            self._pins[page] = snapshot_id

    def switch_after(self, page: int, snapshot_id: str) -> None:
        with self._lock:
            self._switch_after[page] = snapshot_id

    def override_next(self, page: int, snapshot_id: str) -> None:
        with self._lock:
            self._one_shot[page] = snapshot_id

    def replace_page(self, page: SnapshotPage) -> None:
        with self._lock:
            bucket = self._snapshots.setdefault(page.snapshot_id, {})
            bucket[page.url_page or page.page] = page

    def clear_controls(self) -> None:
        with self._lock:
            self._pins.clear()
            self._switch_after.clear()
            self._one_shot.clear()

    def resolve(self, page: int) -> SnapshotPage | None:
        with self._lock:
            snapshot = self._one_shot.pop(page, None)
            if snapshot is None:
                snapshot = self._pins.get(page, self._active)
            if snapshot is None:
                return None
            resolved = self._snapshots.get(snapshot, {}).get(page)
            switch_to = self._switch_after.pop(page, None)
            if switch_to and switch_to in self._snapshots:
                self._active = switch_to
            return resolved


def _card_html(card: dict[str, Any]) -> str:
    title = html.escape(str(card.get("title", "")))
    return (
        f'<li class="job-card" data-job-id="{html.escape(str(card["id"]))}">'
        f'<a class="job-link" href="/jobs/{html.escape(str(card["id"]))}">'
        f'<span class="job-title">{title}</span></a>'
        f'<div class="meta"><span class="job-company">示例公司（虚构）</span>'
        f'<span class="job-location">上海 · 徐汇区</span>'
        f'<span class="job-salary">20-30K</span>'
        f'<span class="job-experience">3-5年</span>'
        f'<span class="job-type">全职</span></div></li>'
    )


def _list_html(page: SnapshotPage) -> str:
    cards = "".join(_card_html(card) for card in page.cards)
    prev_link = (f'<a class="prev-page" href="{html.escape(page.prev_url)}">上一页</a>'
                 if page.prev_url else "")
    next_link = (f'<a class="next-page" rel="next" href="{html.escape(page.next_url)}">下一页</a>'
                 if page.next_url else "")
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>epoch {html.escape(page.snapshot_id)} p{page.page}</title></head><body>"
        f"<div class='page-state' data-snapshot-id='{html.escape(page.snapshot_id)}' "
        f"data-page='{page.page}' data-total-pages='{page.total_pages}'></div>"
        f"<!-- epoch:{html.escape(page.snapshot_id)}:{page.page}:{html.escape(page.marker)} -->"
        f"<ul class='job-list'>{cards}</ul>"
        f"<div class='pager'><span>{prev_link}</span><span>{next_link}</span></div>"
        "</body></html>"
    )


def _detail_html(job_id: str) -> str:
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'></head><body>"
        f"<h1 class='job-title'>合成岗位 {html.escape(job_id)}</h1>"
        "<div class='company-name'>示例公司（虚构）</div>"
        "<div class='job-location'>上海 · 徐汇区</div>"
        "<div class='job-salary'>20-30K</div>"
        "<div class='job-experience'>3-5年</div>"
        "<div class='job-type'>全职</div>"
        "<div class='job-description'>离线合成岗位描述。</div>"
        "<div class='job-posted-at'>2026-01-01</div>"
        "<div class='job-status'>open</div>"
        "<ul class='tags'><li class='tag'>合成</li></ul>"
        "</body></html>"
    )


def _cards(*ids: str) -> tuple[dict[str, Any], ...]:
    return tuple({"id": job_id, "title": f"岗位 {job_id}"} for job_id in ids)


def default_epoch_site() -> EpochSite:
    """Two snapshots over one host: S1 and S2 are both well-formed, 2 pages each."""

    site = EpochSite()
    site.register("S1", [
        SnapshotPage("S1", 1, 2, _cards("S1-A", "S1-B"), next_url="/jobs?page=2"),
        SnapshotPage("S1", 2, 2, _cards("S1-C"), prev_url="/jobs?page=1"),
    ])
    site.register("S2", [
        SnapshotPage("S2", 1, 2, _cards("S2-A", "S2-B"), next_url="/jobs?page=2"),
        SnapshotPage("S2", 2, 2, _cards("S2-C"), prev_url="/jobs?page=1"),
    ])
    return site


def create_epoch_app(site: EpochSite, *, fixtures_dir: str | Path = FIXTURES_DIR) -> Flask:
    app = Flask(__name__)
    app.config["JOBTRACE_EPOCH_SITE"] = site
    app.config["JOBTRACE_FIXTURES_DIR"] = str(fixtures_dir)

    def page_number() -> int:
        try:
            page = int(request.args.get("page", "1"))
        except ValueError:
            abort(400, description="page must be an integer")
        if page < 1:
            abort(400, description="page must be positive")
        return page

    @app.get("/")
    def index():
        return redirect("/jobs?page=1")

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok", "active": site.active})

    @app.get("/jobs")
    def jobs_page():
        resolved = site.resolve(page_number())
        if resolved is None:
            abort(404, description="page not served by any snapshot")
        return _list_html(resolved)

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str):
        return _detail_html(job_id)

    @app.post("/__control/activate")
    def control_activate():
        payload: Any = request.get_json(silent=True) or {}
        try:
            site.activate(str(payload.get("snapshot")))
        except KeyError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"active": site.active})

    @app.post("/__control/pin")
    def control_pin():
        payload: Any = request.get_json(silent=True) or {}
        try:
            site.pin(int(payload["page"]), str(payload["snapshot"]))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "page and snapshot are required"}), 400
        return jsonify({"active": site.active})

    @app.post("/__control/switch_after")
    def control_switch_after():
        payload: Any = request.get_json(silent=True) or {}
        try:
            site.switch_after(int(payload["page"]), str(payload["snapshot"]))
        except (KeyError, TypeError, ValueError):
            return jsonify({"error": "page and snapshot are required"}), 400
        return jsonify({"active": site.active})

    @app.post("/__control/clear")
    def control_clear():
        site.clear_controls()
        return jsonify({"active": site.active})

    return app
