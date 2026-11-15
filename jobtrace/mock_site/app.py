"""Flask mock recruitment site. Loopback-only synthetic content."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, abort, jsonify, make_response, redirect, render_template, request

from ..clock import Clock, SystemClock, iso_z
from ..errors import ConfigError
from .data import FIXTURES_DIR
from .scenarios import ScenarioController


def _fault_response(controller: ScenarioController, status: int, message: str):
    response = make_response(render_template(
        "fault.html", status=status, message=message, scenario=controller.name), status)
    response.headers["Retry-After"] = "0"
    response.headers["Cache-Control"] = "no-store"
    return response


def create_app(scenario: str = "baseline", *, fixtures_dir: str | Path = FIXTURES_DIR,
               page_size: int | None = None, clock: Clock | None = None) -> Flask:
    app = Flask(__name__)
    app.config["JSON_AS_ASCII"] = False
    controller = ScenarioController(scenario, fixtures_dir=fixtures_dir, page_size=page_size)
    app.config["JOBTRACE_CONTROLLER"] = controller
    instant_clock = clock or SystemClock()

    def page_number() -> int:
        raw = request.args.get("page", "1")
        try:
            page = int(raw)
        except ValueError:
            abort(400, description="page must be an integer")
        if page < 1:
            abort(400, description="page must be positive")
        return page

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'unsafe-inline'; img-src 'self' data:"
        )
        return response

    @app.get("/")
    def index():
        return redirect("/jobs?page=1")

    @app.get("/healthz")
    def healthz():
        info = controller.info()
        return jsonify({"status": "ok", "scenario": info["scenario"], "jobs": info["jobs"],
                        "pages": info["pages"], "time": iso_z(instant_clock.now())})

    @app.get("/scenario")
    def scenario_page():
        return render_template("scenario.html", info=controller.info())

    @app.get("/__control/scenarios")
    def list_scenarios():
        info = controller.info()
        return jsonify({"current": info["scenario"], "available": info["available"]})

    @app.post("/__control/scenario")
    def switch_scenario():
        payload: Any = request.get_json(silent=True)
        name = payload.get("scenario") if isinstance(payload, dict) else None
        if not isinstance(name, str):
            return jsonify({"error": "scenario must be a string"}), 400
        try:
            controller.switch(name)
        except ConfigError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"current": controller.name, "info": controller.info()})

    @app.get("/jobs")
    def jobs_page():
        page = page_number()
        if page > controller.total_pages:
            abort(404, description="page out of range")
        if controller.list_fault(page):
            return _fault_response(controller, 503, f"列表页第 {page} 页的注入瞬时故障。")
        cards, next_url = controller.page_cards(page)
        return render_template("list.html", jobs=cards, next_url=next_url, page=page,
                               pages=controller.total_pages, total_jobs=len(controller.data.jobs),
                               scenario=controller.name)

    @app.get("/jobs/<job_id>")
    def job_detail(job_id: str):
        job = controller.job(job_id)
        if job is None or controller.is_missing_detail(job_id):
            abort(404, description="synthetic job detail is intentionally missing")
        if controller.detail_fault(job_id):
            return _fault_response(controller, 503, f"岗位 {job_id} 的详情页注入瞬时故障。")
        return render_template("detail.html", job=job,
                               omitted=controller.omitted_fields(job_id), scenario=controller.name)

    return app
