"""Deterministic Markdown/JSON reports and CSV/JSONL exports."""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any

from .clock import Clock, iso_z, parse_instant
from .fingerprint import canonical_json
from .store import Store

EVENT_TYPES = ("NEW", "CHANGED", "DISAPPEARED", "CLOSED", "REOPENED")
CSV_COLUMNS = (
    "external_id", "title", "company", "location_raw", "city", "district",
    "salary_text", "salary_min", "salary_max", "salary_months", "experience_text",
    "employment_type", "status", "missing_streak", "first_seen_at", "last_seen_at",
    "closed_at", "detail_url",
)


def _timestamp(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return iso_z(parse_instant(value))
    except Exception:
        return value


def _tags(row: dict[str, Any]) -> list[str]:
    try:
        value = json.loads(row.get("tags_json") or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _job_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "job_uid": row["job_uid"], "external_id": row["external_id"],
        "title": row["title"], "company": row["company"],
        "location_raw": row["location_raw"], "city": row["city"],
        "district": row["district"], "salary_text": row["salary_text"],
        "salary_min": row["salary_min"], "salary_max": row["salary_max"],
        "salary_months": row["salary_months"],
        "salary_negotiable": bool(row["salary_negotiable"]),
        "experience_text": row["experience_text"],
        "employment_type": row["employment_type"], "tags": _tags(row),
        "status": row["status"], "missing_streak": row["missing_streak"],
        "reopen_count": row["reopen_count"],
        "first_seen_run_id": row["first_seen_run_id"],
        "first_seen_at": _timestamp(row["first_seen_at"]),
        "last_seen_run_id": row["last_seen_run_id"],
        "last_seen_at": _timestamp(row["last_seen_at"]),
        "closed_at": _timestamp(row["closed_at"]),
        "content_hash": row["content_hash"], "detail_url": row["detail_url"],
    }


def _run_payload(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "run_id": run["run_id"], "scenario": run["scenario"],
        "started_at": _timestamp(run["started_at"]),
        "finished_at": _timestamp(run["finished_at"]),
        "status": run["status"], "is_complete": bool(run["is_complete"]),
        "pages_fetched": run["pages_fetched"], "list_cards": run["list_cards"],
        "unique_jobs": run["unique_jobs"], "details_ok": run["details_ok"],
        "details_failed": run["details_failed"],
        "duplicate_ids": run["duplicate_ids"],
        "parse_warnings": run["parse_warnings"], "resumed": bool(run["resumed"]),
        "stop_reason": run["stop_reason"], "config_hash": run["config_hash"],
    }


def _event_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "event_type": row["event_type"], "field": row["field"],
        "old_value": row["old_value"], "new_value": row["new_value"],
        "created_at": _timestamp(row["created_at"]), "job_uid": row["job_uid"],
        "external_id": row["external_id"], "title": row["title"],
        "company": row["company"],
    }


def _sampling_payload(sampling: Any) -> dict[str, Any] | None:
    """Report only the contract, counts, coverage, and inference status of a run."""
    if not isinstance(sampling, dict) or not sampling.get("contract"):
        return None
    counts = sampling.get("counts") or {}
    estimate = sampling.get("estimate") or {}
    return {
        "contract": sampling.get("contract"),
        "budget": sampling.get("budget"), "seed": sampling.get("seed"),
        "stratum_field": sampling.get("stratum_field"),
        "allocations": sampling.get("allocations") or {},
        "uncovered_strata": sampling.get("uncovered_strata") or {},
        "uncovered_ids": sampling.get("uncovered_ids") or [],
        "counts": {
            key: counts.get(key, 0)
            for key in ("list_discovered", "list_unique", "duplicates_removed",
                        "detail_attempted", "detail_succeeded", "detail_failed",
                        "verified", "verified_positive", "unknown_field", "not_attempted")
        },
        "estimate": {
            "intern_count": estimate.get("intern_count"),
            "proportion": estimate.get("proportion"),
            "label": estimate.get("label"),
        } if estimate else None,
        "bounds": sampling.get("bounds"),
        "variance_estimable": bool(sampling.get("variance_estimable")),
        "variance_reason": sampling.get("variance_reason"),
        "claims": sampling.get("claims") or {},
    }


def build_report(store: Store, clock: Clock, site_key: str | None = None) -> dict[str, Any]:
    latest = store.latest_run(site_key)
    selected_site = site_key or (latest["site_key"] if latest else "")
    ledger = store.ledger_counts(selected_site)
    runs = [_run_payload(run) for run in store.list_runs(selected_site, limit=20)]
    jobs = [_job_payload(row) for row in store.list_jobs(site_key=selected_site, limit=10000)]
    events: list[dict[str, Any]] = []
    counts = {name: 0 for name in EVENT_TYPES}
    if latest:
        counts = store.event_counts(latest["run_id"])
        events = [_event_payload(row) for row in store.list_events(run_id=latest["run_id"], limit=10000)]
    summary = {
        "site_key": selected_site, "jobs_total": ledger["total"],
        "jobs_open": ledger.get("open", 0), "jobs_closed": ledger.get("closed", 0),
        "run_id": latest["run_id"] if latest else None,
        "run_status": latest["status"] if latest else "none",
        "run_complete": bool(latest["is_complete"]) if latest else False,
        "run_scenario": latest["scenario"] if latest else None,
        "run_started_at": _timestamp(latest["started_at"]) if latest else None,
        "run_finished_at": _timestamp(latest["finished_at"]) if latest else None,
        "event_counts": counts,
    }
    latest_payload = _run_payload(latest) if latest else None
    if latest_payload:
        notes = store.notes(latest)
        latest_payload.update(
            {key: notes.get(key, {} if key == "selector_misses" else [])
             for key in ("lifecycle", "failures", "warnings", "selector_misses")}
        )
        latest_payload["sampling"] = _sampling_payload(notes.get("sampling"))
    raw_count = len(store.raw_documents_for_run(latest["run_id"])) if latest else 0
    return {
        "schema_version": 1, "generated_at": iso_z(clock.now()),
        "site_key": selected_site, "summary": summary,
        "latest_run": latest_payload, "runs": runs, "jobs": jobs, "events": events,
        "raw_evidence": {"documents_in_latest_run": raw_count},
    }


def _render_sampling(lines: list[str], sampling: dict[str, Any] | None) -> None:
    if not sampling:
        return
    counts = sampling.get("counts") or {}
    estimate = sampling.get("estimate") or {}
    bounds = sampling.get("bounds") or {}
    claims = sampling.get("claims") or {}
    covered = sampling.get("uncovered_strata") or {}
    contract = sampling.get("contract")
    lines.extend([
        "", "## 有界详情抽样", "",
        f"- 合同：`{contract}`；分层字段：`{sampling.get('stratum_field')}`；"
        f"预算：{sampling.get('budget')}；种子：{sampling.get('seed')}",
        f"- 列表发现数：{counts.get('list_discovered', 0)}；去重后样本框："
        f"{counts.get('list_unique', 0)}；重复 ID：{counts.get('duplicates_removed', 0)}",
        f"- 详情尝试数：{counts.get('detail_attempted', 0)}；成功样本数："
        f"{counts.get('detail_succeeded', 0)}；失败/未知：{counts.get('detail_failed', 0)}；"
        f"已验证个数：{counts.get('verified', 0)}；未尝试：{counts.get('not_attempted', 0)}",
        f"- 未覆盖层：`{canonical_json(covered)}`；未覆盖 ID："
        f"`{canonical_json(sampling.get('uncovered_ids') or [])}`",
        f"- 覆盖范围：`{claims.get('coverage_scope')}`；已跑完全部详情："
        f"`{str(claims.get('details_complete')).lower()}`；"
        f"是否可声称整页无偏比例："
        f"`{str(claims.get('whole_page_unbiased_proportion')).lower()}`",
    ])
    if estimate.get("intern_count") is not None:
        lines.append(f"- 设计加权估计（实习数）：{estimate['intern_count']}"
                     f"（{estimate.get('label')}），未经验证的个数不计为 0。")
    elif contract == "probability":
        lines.append("- 设计加权估计：不可得（无偏概率样本不完整或有失败/未知结果）。")
    else:
        lines.append("- 设计加权估计：定向合同不提供整页推断，仅陈述所列 ID 的真实字段。")
    if bounds:
        lines.append(f"- 可证上下界（整页实习数）：{bounds.get('intern_count')}；"
                     f"比例：{bounds.get('proportion')}")
    if contract == "probability":
        reason = sampling.get("variance_reason")
        lines.append("- 方差不可估" if not sampling.get("variance_estimable")
                     else "- 方差可估")
        if reason:
            lines.append(f"- 原因：{reason}")
    lines.append(f"- 说明：{claims.get('note')}")


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary, latest = report["summary"], report["latest_run"]
    lines = [
        "# 职迹采采集与生命周期报告", "",
        "> 仅含仓库内置的合成岗位数据；不包含个人信息或外部网络内容。", "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 站点：`{summary['site_key'] or '(无)'}`",
        f"- 台账岗位：{summary['jobs_total']}（开放 {summary['jobs_open']} / 关闭 {summary['jobs_closed']}）",
    ]
    if latest:
        lines.extend([
            f"- 最新运行：`{latest['run_id']}`（场景 `{latest['scenario']}`）",
            f"- 运行状态：`{latest['status']}`，完整运行：`{str(latest['is_complete']).lower()}`",
            f"- 列表页：{latest['pages_fetched']}，列表卡片：{latest['list_cards']}，"
            f"详情成功：{latest['details_ok']}，详情失败：{latest['details_failed']}",
        ])
    counts = summary["event_counts"]
    lines.extend(["", "## 生命周期事件计数", "", "| 事件 | 数量 |", "| --- | ---: |"])
    lines.extend(f"| {name} | {counts.get(name, 0)} |" for name in EVENT_TYPES)
    _render_sampling(lines, latest.get("sampling") if latest else None)
    lines.extend(["", "## 最新运行事件", ""])
    if report["events"]:
        lines.extend(["| 类型 | 岗位 | 字段 | 旧值 | 新值 | 时间 |", "| --- | --- | --- | --- | --- | --- |"])
        for event in report["events"]:
            values = (
                event["event_type"], _cell(f"{event['external_id']} {event['title']}"),
                _cell(event["field"]), _cell(event["old_value"]), _cell(event["new_value"]),
                event["created_at"],
            )
            lines.append("| {} | {} | {} | {} | {} | {} |".format(*values))
    else:
        lines.append("本次运行没有生命周期事件。")
    lines.extend([
        "", "## 岗位台账", "",
        "| 编号 | 标题 | 公司 | 城市 | 薪资 | 状态 | 首次出现 | 最近出现 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for job in report["jobs"]:
        values = (
            job["external_id"], _cell(job["title"]), _cell(job["company"]),
            _cell(job["city"]), _cell(job["salary_text"]), job["status"],
            job["first_seen_at"], job["last_seen_at"],
        )
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(*values))
    lines.extend([
        "", "## 证据与诊断", "",
        f"- 原始响应文档数：{report['raw_evidence']['documents_in_latest_run']}",
        f"- 选择器缺失统计：`{canonical_json(latest.get('selector_misses', {}) if latest else {})}`",
        "",
    ])
    return "\n".join(lines)


def _csv_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "、".join(str(item) for item in value)
    return "" if value is None else value


def write_reports(
    store: Store, output_dir: str | Path, clock: Clock, site_key: str | None = None
) -> dict[str, str]:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    report = build_report(store, clock, site_key)
    paths = {
        "markdown": output / "report.md", "json": output / "report.json",
        "csv": output / "jobs.csv", "jsonl": output / "events.jsonl",
    }
    paths["json"].write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    paths["markdown"].write_text(render_markdown(report) + "\n", encoding="utf-8")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for job in report["jobs"]:
        writer.writerow({key: _csv_value(job.get(key)) for key in CSV_COLUMNS})
    paths["csv"].write_text(stream.getvalue(), encoding="utf-8", newline="")
    with paths["jsonl"].open("w", encoding="utf-8", newline="\n") as handle:
        for event in report["events"]:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
    return {key: str(path.resolve()) for key, path in paths.items()}
