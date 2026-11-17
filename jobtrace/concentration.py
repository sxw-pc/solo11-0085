"""Single-run employer concentration audit over stored job snapshots.

只读分析：显式选择一个完整 run 的岗位快照，按受控合成雇主注册表把展示名解析为
法律雇主/管理集团候选，输出刊登份额 HHI 的可行区间与完整 job_uid→雇主指派见证。
不修改 jobs、events 或快照；不把跨站相似岗位合并为一个招聘需求。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from fractions import Fraction
from itertools import product
from typing import Any

from .clock import iso_z
from .employers import EmployerRegistry
from .fingerprint import canonical_json, sha256_text
from .normalize import clean_text
from .store import Store

RULES_VERSION = "1"
SCHEMA = "employer-concentration/1"
MAX_ASSIGNMENTS = 200_000

CONTRACTS = {
    "legal": ("legal_employer", "法律雇主合同"),
    "group": ("management_group", "集团管控合同"),
}


def _run_brief(run: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": run["status"], "is_complete": bool(run["is_complete"]),
        "started_at": run["started_at"], "finished_at": run["finished_at"],
        "unique_jobs": run["unique_jobs"], "details_failed": run["details_failed"],
    }


def _contract_shell(status: str, denominator: int | None) -> dict[str, Any]:
    return {
        key: {"contract": names[0], "label": names[1], "status": status,
              "denominator": denominator, "hhi": None, "witnesses": None}
        for key, names in CONTRACTS.items()
    }


def _abort(base: dict[str, Any], status: str, detail: str,
           run: dict[str, Any] | None = None,
           postings: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        **base, "status": status, "run": _run_brief(run) if run else None,
        "reasons": [{"code": status, "detail": detail}], "postings": postings,
        "tokens": [], "unknown_companies": [], "input_fingerprint": None,
        "contracts": _contract_shell(status, None),
    }


def _rounded(value: Fraction) -> float:
    return float(round(value, 6))


def _hhi(counts: dict[str, int], denominator: int) -> Fraction:
    return Fraction(sum(count * count for count in counts.values()), denominator ** 2)


def _entity_name(registry: EmployerRegistry, contract: str, entity: str) -> str:
    if contract == "group" and entity in registry.groups:
        return registry.groups[entity]
    return registry.employers[entity].legal_name


def _resolve(registry: EmployerRegistry, snapshots: list[dict[str, Any]]):
    token_jobs: dict[tuple[str, str], list[str]] = {}
    unknown: dict[tuple[str, str], dict[str, Any]] = {}
    fingerprint_rows: list[list[str]] = []
    for row in snapshots:
        try:
            payload = json.loads(row["payload_json"])
            company = clean_text(str(payload.get("company") or ""))
        except (TypeError, json.JSONDecodeError):
            company = ""
        day = str(row["observed_at"])[:10]
        fingerprint_rows.append([row["job_uid"], row["content_hash"], company, day])
        reason = ""
        if not company:
            reason = "missing_company"
        else:
            try:
                candidates = registry.candidates(company, date.fromisoformat(day))
            except ValueError:
                candidates = ()
            if not candidates:
                reason = "unknown_company"
        if reason:
            entry = unknown.setdefault(
                (company, reason),
                {"display": company, "reason": reason, "postings": 0, "job_uids": []})
            entry["postings"] += 1
            entry["job_uids"].append(row["job_uid"])
        else:
            token_jobs.setdefault((company, day), []).append(row["job_uid"])
    unknown_list = [
        {**entry, "job_uids": sorted(entry["job_uids"])}
        for _, entry in sorted(unknown.items())
    ]
    fingerprint = sha256_text(canonical_json(sorted(fingerprint_rows)))
    return token_jobs, unknown_list, fingerprint


def _mutex_pairs(registry: EmployerRegistry, tokens: list[tuple[str, str]]):
    by_display: dict[str, list[tuple[str, str]]] = {}
    for token in tokens:
        by_display.setdefault(token[0], []).append(token)
    return [(a, b) for left, right in registry.mutex
            for a in by_display.get(clean_text(left), ())
            for b in by_display.get(clean_text(right), ())]


def _assignment_bounds(registry: EmployerRegistry, token_jobs, denominator: int):
    """Enumerate global alias->employer assignments; None=too large, {}=infeasible."""
    tokens = sorted(token_jobs)
    candidate_lists = [registry.candidates(display, date.fromisoformat(day))
                       for display, day in tokens]
    space = 1
    for candidates in candidate_lists:
        space *= len(candidates)
    if space > MAX_ASSIGNMENTS:
        return None
    pairs = _mutex_pairs(registry, tokens)
    best: dict[str, dict[str, tuple[Fraction, dict] | None]] = {
        key: {"min": None, "max": None} for key in CONTRACTS
    }
    for combo in product(*candidate_lists):
        assignment = dict(zip(tokens, combo))
        if any(assignment[a] == assignment[b] for a, b in pairs):
            continue
        counts: dict[str, dict[str, int]] = {"legal": {}, "group": {}}
        for token, employer_id in assignment.items():
            postings = len(token_jobs[token])
            counts["legal"][employer_id] = counts["legal"].get(employer_id, 0) + postings
            group = registry.group_of(employer_id)
            counts["group"][group] = counts["group"].get(group, 0) + postings
        for key in CONTRACTS:
            value = _hhi(counts[key], denominator)
            for bound, is_better in (("min", Fraction.__lt__), ("max", Fraction.__gt__)):
                current = best[key][bound]
                if current is None or is_better(value, current[0]):
                    best[key][bound] = (value, assignment)
    if best["legal"]["min"] is None:
        return {}
    return best


def _witness(registry: EmployerRegistry, contract: str, assignment: dict,
             token_jobs, denominator: int) -> dict[str, Any]:
    jobs: dict[str, str] = {}
    counts: dict[str, int] = {}
    for token in sorted(assignment):
        employer_id = assignment[token]
        entity = employer_id if contract == "legal" else registry.group_of(employer_id)
        for job_uid in token_jobs[token]:
            jobs[job_uid] = entity
        counts[entity] = counts.get(entity, 0) + len(token_jobs[token])
    shares = [
        {"id": entity, "name": _entity_name(registry, contract, entity),
         "postings": count, "share": _rounded(Fraction(count, denominator))}
        for entity, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    ]
    return {
        "assignment": [
            {"display": display, "date": day, "employer": assignment[(display, day)]}
            for display, day in sorted(assignment)
        ],
        "jobs": dict(sorted(jobs.items())), "shares": shares,
        "hhi": _rounded(_hhi(counts, denominator)),
    }


def analyze_run(store: Store, run_id: str, registry: EmployerRegistry, *,
                now: datetime | None = None) -> dict[str, Any]:
    """Analyze one explicitly selected complete run; never writes to the store."""
    base = {
        "schema": SCHEMA, "rules_version": RULES_VERSION,
        "registry_version": registry.version,
        "generated_at": iso_z(now or datetime.now(timezone.utc)), "run_id": run_id,
    }
    run = store.get_run(run_id)
    if run is None:
        return _abort(base, "run_not_found", "run 不存在，无法选择其岗位快照")
    snapshots = store.run_snapshots(run_id)
    postings = {"total": len(snapshots), "known": 0, "unknown": 0,
                "unobserved": max(0, int(run["unique_jobs"]) - len(snapshots))}
    if not (run["finished_at"] and run["status"] == "complete" and run["is_complete"]):
        return _abort(base, "incomplete_run",
                      "run 未完整完成，不能当作完整市场快照", run, postings)
    if not snapshots:
        return _abort(base, "empty", "完整 run 内没有岗位快照，分母为零", run, postings)
    token_jobs, unknown_list, fingerprint = _resolve(registry, snapshots)
    postings["known"] = sum(len(jobs) for jobs in token_jobs.values())
    postings["unknown"] = postings["total"] - postings["known"]
    tokens_payload = [
        {"display": display, "date": day,
         "candidates": list(registry.candidates(display, date.fromisoformat(day))),
         "postings": len(token_jobs[(display, day)])}
        for display, day in sorted(token_jobs)
    ]
    common = {**base, "run": _run_brief(run), "postings": postings,
              "tokens": tokens_payload, "unknown_companies": unknown_list,
              "input_fingerprint": fingerprint}
    if unknown_list or postings["unobserved"]:
        reasons = [
            {"code": entry["reason"],
             "detail": f"「{entry['display'] or '空公司名'}」有 {entry['postings']} 个刊登无法解析"}
            for entry in unknown_list
        ]
        if postings["unobserved"]:
            reasons.append({"code": "coverage_gap",
                            "detail": f"有 {postings['unobserved']} 个已发现岗位缺少详情观测"})
        return {**common, "status": "unresolvable", "reasons": reasons,
                "contracts": _contract_shell("unresolvable", postings["total"])}
    bounds = _assignment_bounds(registry, token_jobs, postings["total"])
    if bounds is None:
        return _abort(base, "too_complex", "候选指派空间过大，拒绝枚举", run, postings)
    if not bounds:
        return {**common, "status": "infeasible",
                "reasons": [{"code": "mutex_infeasible",
                             "detail": "受控互斥关系排除了全部候选指派"}],
                "contracts": _contract_shell("infeasible", postings["total"])}
    reasons = [
        {"code": "ambiguous_alias",
         "detail": f"「{token['display']}」在 {token['date']} 可指 "
                   f"{'/'.join(token['candidates'])}，HHI 以可行区间给出"}
        for token in tokens_payload if len(token["candidates"]) > 1
    ]
    contracts = {}
    for key, (contract_id, label) in CONTRACTS.items():
        low_value, low_assignment = bounds[key]["min"]
        high_value, high_assignment = bounds[key]["max"]
        contracts[key] = {
            "contract": contract_id, "label": label, "status": "ok",
            "denominator": postings["total"],
            "hhi": {"min": _rounded(low_value), "max": _rounded(high_value),
                    "point": low_value == high_value},
            "witnesses": {
                "min": _witness(registry, key, low_assignment, token_jobs, postings["total"]),
                "max": _witness(registry, key, high_assignment, token_jobs, postings["total"]),
            },
        }
    return {**common, "status": "ok", "reasons": reasons, "contracts": contracts}
