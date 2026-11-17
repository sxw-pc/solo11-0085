"""Read-only compensation explanations and cross-job comparison queries.

Everything here opens data through the existing Store read path (jobs rows and
job_snapshots payloads). No lifecycle table, event or stored salary column is
written or rewritten; the legacy salary_min/salary_max ordering is never used.
"""

from __future__ import annotations

import json
from typing import Any

from .compensation import (
    CompensationError, Feasibility, analyze_feasibility, parse_compensation,
)
from .comparison import (
    OPPORTUNITY, ROBUST, ComparisonVerdict, compare_opportunity, compare_robust,
    explain_feasibility,
)
from .store import Store

_SNAPSHOT_SALARY_KEYS = ("salary_text",)


def _raw_salary_candidates(row: dict[str, Any], store: Store) -> list[str]:
    """Recover the original salary text, preferring the stored snapshot payload."""

    candidates: list[str] = []
    for snapshot in store.list_snapshots(row["job_uid"]):
        try:
            payload = json.loads(snapshot.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        for key in _SNAPSHOT_SALARY_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value:
                candidates.append(value)
    fallback = row.get("salary_text")
    if isinstance(fallback, str) and fallback and fallback not in candidates:
        candidates.append(fallback)
    return candidates


def explain_job(row: dict[str, Any], store: Store) -> dict[str, Any]:
    """Explain one stored job's salary constraints from read-only data."""

    base = {
        "job_uid": row["job_uid"], "external_id": row["external_id"],
        "title": row["title"], "company": row["company"],
        "stored_salary_text": row.get("salary_text") or "",
    }
    for text in _raw_salary_candidates(row, store):
        try:
            plan = parse_compensation(text)
        except CompensationError:
            continue
        result = analyze_feasibility(plan)
        explanation = explain_feasibility(result)
        base.update({"source_text": text, **explanation})
        return base
    base.update({"source_text": "", "feasible": False,
                 "parse_errors": ["快照与台账中没有可解析的薪酬原文"],
                 "conflicts": []})
    return base


def explain_store(store: Store, *, site_key: str | None = None,
                  limit: int = 10000) -> list[dict[str, Any]]:
    return [explain_job(row, store)
            for row in store.list_jobs(site_key=site_key, limit=limit)]


def _feasibility_from_text(text: str) -> Feasibility:
    return analyze_feasibility(parse_compensation(text))


def compare_jobs(store: Store, external_id_a: str, external_id_b: str,
                 *, site_key: str | None = None) -> dict[str, Any]:
    """Run both comparison contracts against two read-only stored jobs."""

    rows = {row["external_id"]: row
            for row in store.list_jobs(site_key=site_key, limit=10000)}
    payload: dict[str, Any] = {"a": external_id_a, "b": external_id_b}
    missing = [key for key, value in (("a", external_id_a), ("b", external_id_b))
               if value not in rows]
    if missing:
        payload["error"] = f"岗位不存在: {', '.join(missing)}"
        return payload
    texts = {}
    for key, external_id in (("a", external_id_a), ("b", external_id_b)):
        candidates = _raw_salary_candidates(rows[external_id], store)
        if not candidates:
            payload["error"] = f"岗位 {external_id} 缺少薪酬原文"
            return payload
        texts[key] = candidates[0]
    left, right = _feasibility_from_text(texts["a"]), _feasibility_from_text(texts["b"])
    payload["sources"] = {"a": texts["a"], "b": texts["b"]}
    payload["feasibility"] = {"a": explain_feasibility(left),
                              "b": explain_feasibility(right)}
    payload["contracts"] = {
        ROBUST: _verdict_dict(compare_robust(left, right)),
        OPPORTUNITY: _verdict_dict(compare_opportunity(left, right)),
    }
    return payload


def _verdict_dict(verdict: ComparisonVerdict) -> dict[str, Any]:
    return {
        "contract": verdict.contract, "verdict": verdict.verdict,
        "holds": verdict.holds, "reason": verdict.reason,
        "witness": verdict.witness, "feasible_intervals": verdict.feasible,
        "currencies": verdict.currencies,
    }


def render_compensation_markdown(entries: list[dict[str, Any]]) -> str:
    """Deterministic Markdown section; existing report sections are untouched."""

    lines = ["", "## 薪酬约束解释（精确有理数，离线合成）", ""]
    if not entries:
        lines.append("没有可解释的岗位薪酬。")
        return "\n".join(lines)
    lines.extend([
        "| 编号 | 原文 | 币种 | 严格/保证组成 | 目标组成 | 年包约束 | 可行年度收入 | 冲突 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for entry in entries:
        conflicts = "；".join(
            f"{c['left']}∩{c['right']}: {c['detail']}" for c in entry.get("conflicts", [])
        ) or "无"
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
            entry["external_id"], _cell(entry.get("source_text", "")),
            entry.get("currency", "-"),
            _cell(entry.get("strict_guaranteed_interval", "-")),
            _cell(entry.get("target_variable_interval", "-")),
            _cell(entry.get("package_constraint") or "无"),
            _cell(entry.get("feasible_annual_interval") or "无解"),
            _cell(conflicts),
        ))
    lines.extend([
        "", "> 说明：年包总额注明“含上述各项”时与各组成之和取交集而非相加；",
        "目标/绩效奖金与提成不是保证收入；“上不封顶”保留无穷上界；",
        "不同币种在缺少本地换算依据时拒绝比较。", "",
    ])
    return "\n".join(lines)


def render_comparison_markdown(result: dict[str, Any]) -> str:
    lines = ["", "## 跨岗位薪酬比较（两种语义合同）", ""]
    if "error" in result:
        lines.append(f"- 比较拒绝：{result['error']}")
        return "\n".join(lines)
    lines.append(f"- A：`{result['a']}` 原文：{result['sources']['a']}")
    lines.append(f"- B：`{result['b']}` 原文：{result['sources']['b']}")
    names = {ROBUST: "稳健优势（每个 A 可行收入严格高于每个 B 可行收入）",
             OPPORTUNITY: "机会优势（存在 A 可行收入高于某 B 可行收入，非保证）"}
    lines.extend(["", "| 合同 | 结论 | 理由 | 数值见证 |", "| --- | --- | --- | --- |"])
    for key in (ROBUST, OPPORTUNITY):
        verdict = result["contracts"][key]
        witness = "；".join(f"{k}={v}" for k, v in verdict["witness"].items()) or "无"
        lines.append("| {} | {} | {} | {} |".format(
            names[key], verdict["verdict"], _cell(verdict["reason"]), _cell(witness),
        ))
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str(value if value is not None else "").replace("|", "\\|").replace("\n", " ")
