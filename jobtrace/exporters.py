"""Deterministic four-format rendering from one in-memory report object."""

from __future__ import annotations

import csv
import io
import json
from typing import Any

from .fingerprint import canonical_json

CSV_COLUMNS = (
    "external_id", "title", "company", "location_raw", "city", "district",
    "salary_text", "salary_min", "salary_max", "salary_months", "experience_text",
    "employment_type", "status", "missing_streak", "first_seen_at", "last_seen_at",
    "closed_at", "detail_url",
)
IDENTITY_COLUMNS = ("bundle_id", "content_digest")
EXPORT_COLUMNS = CSV_COLUMNS + IDENTITY_COLUMNS
EVENT_TYPES = ("NEW", "CHANGED", "DISAPPEARED", "CLOSED", "REOPENED")


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")


def render_markdown(report: dict[str, Any]) -> str:
    summary, latest, bundle = report["summary"], report["latest_run"], report["bundle"]
    lines = [
        "# 职迹采采集与生命周期报告", "",
        f"<!-- jobtrace-bundle: {bundle['bundle_id']} sha256:{bundle['content_digest']} -->",
        "",
        "> 仅含仓库内置的合成岗位数据；不包含个人信息或外部网络内容。", "",
        f"- 生成时间：`{report['generated_at']}`",
        f"- 报告批次：`{bundle['bundle_id']}`",
        f"- 内容摘要：`sha256:{bundle['content_digest']}`",
        f"- 观察范围：`{bundle['observation_scope']}`",
        f"- 站点：`{summary['site_key'] or '(无)'}`",
        f"- 台账岗位：{summary['jobs_total']}（开放 {summary['jobs_open']} / 关闭 {summary['jobs_closed']}）",
    ]
    if not bundle["observation_complete"]:
        lines.append("- **注意：本次采集不完整，此批次不代表完整岗位集合。**")
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


def render_csv(report: dict[str, Any]) -> str:
    bundle = report["bundle"]
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=EXPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    identity = {"bundle_id": bundle["bundle_id"],
                "content_digest": bundle["content_digest"]}
    for job in report["jobs"]:
        row = {key: _csv_value(job.get(key)) for key in CSV_COLUMNS}
        row.update(identity)
        writer.writerow(row)
    if not report["jobs"]:
        # Identity must be checkable even with an empty observation version.
        writer.writerow({**{key: "" for key in CSV_COLUMNS}, **identity})
    return stream.getvalue()


def render_jsonl(report: dict[str, Any]) -> str:
    bundle = report["bundle"]
    if not report["events"]:
        # A zero-event observation version still carries its own identity.
        return json.dumps({"_bundle": {
            "bundle_id": bundle["bundle_id"],
            "content_digest": bundle["content_digest"],
        }}, ensure_ascii=False, sort_keys=True) + "\n"
    return "".join(
        json.dumps({**event, "bundle_id": bundle["bundle_id"],
                    "content_digest": bundle["content_digest"]},
                   ensure_ascii=False, sort_keys=True) + "\n"
        for event in report["events"]
    )


def render_bundle(report: dict[str, Any]) -> dict[str, str]:
    """Render all four formats from one in-memory report object."""

    return {
        "json": render_json(report),
        "markdown": render_markdown(report) + "\n",
        "csv": render_csv(report),
        "jsonl": render_jsonl(report),
    }

