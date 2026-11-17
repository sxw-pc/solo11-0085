"""Minimal loopback site with controllable per-page encodings and headers.

Unlike the scenario-driven mock site, every byte here is assembled directly so
charset acceptance tests can mislabel HTTP headers while keeping document
declarations honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from flask import Flask, make_response, request

GB18030 = "gb18030"
UTF8 = "utf-8"
TITLE = "工程师"
COMPANY = "甲公司"


@dataclass
class CharsetPage:
    body: bytes
    content_type: str
    status: int = 200


@dataclass
class CharsetSite:
    """Routes mapped to fully prepared response bytes."""

    pages: dict[str, CharsetPage] = field(default_factory=dict)

    def set(self, path: str, body: bytes, content_type: str, status: int = 200) -> None:
        self.pages[path] = CharsetPage(body=body, content_type=content_type, status=status)


def build_list_html(*, meta_charset: str | None) -> bytes:
    declared = f'<meta charset="{meta_charset}">' if meta_charset else ""
    return (
        b'<!doctype html><html lang="zh-CN"><head>' + declared.encode("ascii")
        + b"<title>jobs</title></head><body>"
        + b'<ul><li class="job-card" data-job-id="7">'
        + b'<a class="job-link" href="/jobs/7">'
        + b'<span class="job-title">list-title</span></a>'
        + b'<div><span class="job-company">list-company</span>'
        + b'<span class="job-location">SH</span>'
        + b'<span class="job-salary">10k</span></div></li></ul>'
        b"</body></html>"
    )


def build_detail_html(*, title: str, company: str, encoding: str,
                      meta_charset: str | None) -> bytes:
    declared = f'<meta charset="{meta_charset}">' if meta_charset else ""
    template = (
        '<!doctype html><html lang="zh-CN"><head>{decl}<title>detail</title></head>'
        '<body><h1 class="job-title">{title}</h1>'
        '<dl><dt>c</dt><dd class="company-name">{company}</dd></dl>'
        '<p class="job-location">SH</p><p class="job-salary">10k-20k</p>'
        '<p class="job-experience">3y</p><p class="job-type">fulltime</p>'
        '<p class="job-description">desc</p><p class="job-posted-at">2026-01-15</p>'
        '<p class="job-status">open</p><ul class="tags">'
        '<li class="tag">py</li></ul></body></html>'
    )
    return template.format(decl=declared, title=title, company=company).encode(encoding)


def create_charset_app(site: CharsetSite) -> Flask:
    app = Flask(__name__)

    @app.get("/jobs")
    def jobs():
        page = site.pages.get("/jobs")
        if page is None:
            return "not configured", 500
        response = make_response(page.body, page.status)
        response.headers["Content-Type"] = page.content_type
        return response

    @app.get("/jobs/<job_id>")
    def detail(job_id: str):
        page = site.pages.get(f"/jobs/{job_id}")
        if page is None:
            return "not configured", 404
        response = make_response(page.body, page.status)
        response.headers["Content-Type"] = page.content_type
        return response

    @app.before_request
    def _only_known_paths():
        if request.path not in site.pages:
            return "unknown route", 404

    return app
