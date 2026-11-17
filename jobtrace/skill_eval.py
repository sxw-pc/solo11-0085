"""技能经验资格求值引擎：接触/专属两种合同与全局可行分配判定。

专属投入合同用分数精确的最大流（Edmonds-Karp）判定“所有技能下限是否存在一组全局
可行分配”，失败时由残余图给出容量冲突见证（饱和月份、争用月份、缺口），
不按子句顺序贪心，也不用两个局部累计通过冒充合格。
"""

from __future__ import annotations

from collections import deque
from fractions import Fraction
from typing import Any, Iterable

from .skill_exp import (
    NO_EVIDENCE, QUALIFIED, SATISFIED, UNMET, UNQUALIFIED, ClauseAudit,
    Ledger, QualificationReport, RequirementError, ResumeEntry, SkillRequirement,
    parse_requirement, requirement_from_dict, validate_entries, entry_from_dict,
    CONTRACTS,
)


def _eval_exposure(ledger: Ledger, req: SkillRequirement) -> tuple[ClauseAudit, ...]:
    audits = []
    for clause in req.clauses:
        evidence = tuple(
            {"month": month, "amount": str(amount),
             "segments": [seg.seg_id for seg, _ in ledger.by_month[month]
                          if clause.key in seg.skills]}
            for month, amount in sorted(ledger.supply.get(clause.key, {}).items()))
        if not evidence:
            status = NO_EVIDENCE
        else:
            status = SATISFIED if len(evidence) >= clause.min_months else UNMET
        audits.append(ClauseAudit(clause.key, clause.label, clause.min_months,
                                  status, str(len(evidence)), evidence))
    return tuple(audits)


def _residual(graph: dict, flow: dict, u: Any, v: Any) -> Fraction:
    return graph.get(u, {}).get(v, Fraction(0)) - flow.get(u, {}).get(v, Fraction(0))


def _neighbors(graph: dict, flow: dict, node: Any) -> list[Any]:
    return sorted(set(graph.get(node, ())) | set(flow.get(node, ())), key=str)


def _max_flow(graph: dict, source: Any, sink: Any) -> tuple[Fraction, dict]:
    flow: dict[Any, dict[Any, Fraction]] = {}
    total = Fraction(0)
    while True:
        parent = {source: None}
        queue = deque([source])
        while queue and sink not in parent:
            node = queue.popleft()
            for nxt in _neighbors(graph, flow, node):
                if nxt not in parent and _residual(graph, flow, node, nxt) > 0:
                    parent[nxt] = node
                    queue.append(nxt)
        if sink not in parent:
            return total, flow
        path, node = [], sink
        while parent[node] is not None:
            path.append((parent[node], node))
            node = parent[node]
        bottleneck = min(_residual(graph, flow, u, v) for u, v in path)
        for u, v in path:
            row = flow.setdefault(u, {})
            row[v] = row.get(v, Fraction(0)) + bottleneck
            back = flow.setdefault(v, {})
            back[u] = back.get(u, Fraction(0)) - bottleneck
        total += bottleneck


def _reachable(graph: dict, flow: dict, source: Any) -> set[Any]:
    seen, queue = {source}, deque([source])
    while queue:
        node = queue.popleft()
        for nxt in _neighbors(graph, flow, node):
            if nxt not in seen and _residual(graph, flow, node, nxt) > 0:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def _eval_dedicated(ledger: Ledger, req: SkillRequirement,
                    ) -> tuple[tuple[ClauseAudit, ...], dict[str, Any] | None]:
    source, sink = "source", "sink"
    graph: dict[Any, dict[Any, Fraction]] = {}
    keys = {clause.key for clause in req.clauses}

    def link(u: Any, v: Any, amount: Fraction) -> None:
        if amount > 0:
            graph.setdefault(u, {})[v] = graph.setdefault(u, {}).get(v, Fraction(0)) + amount
            graph.setdefault(v, {})

    for month in ledger.months:
        link(source, ("month", month), ledger.capacity[month])
        for seg, amount in ledger.by_month[month]:
            node = ("seg", seg.seg_id, month)
            link(("month", month), node, amount)
            for skill in keys.intersection(seg.skills):
                link(node, ("skill", skill), amount)
    demand = sum((Fraction(c.min_months) for c in req.clauses), Fraction(0))
    for clause in req.clauses:
        link(("skill", clause.key), sink, Fraction(clause.min_months))
    total, flow = _max_flow(graph, source, sink)
    audits = []
    for clause in req.clauses:
        upper = sum((min(ledger.capacity[m], ledger.supply.get(clause.key, {}).get(m, Fraction(0)))
                     for m in ledger.months), Fraction(0))
        evidence = tuple(
            {"month": month, "amount": str(used), "segments": [seg.seg_id]}
            for month in ledger.months for seg, _ in ledger.by_month[month]
            if (used := flow.get(("seg", seg.seg_id, month), {}).get(
                ("skill", clause.key), Fraction(0))) > 0)
        if total >= demand:
            status = SATISFIED
        else:
            status = NO_EVIDENCE if upper == 0 else UNMET
        audits.append(ClauseAudit(clause.key, clause.label, clause.min_months,
                                  status, str(upper), evidence))
    witness = None
    if total < demand:
        reach = _reachable(graph, flow, source)
        witness = {
            "demand_total": str(demand), "allocated_total": str(total),
            "deficit": str(demand - total),
            "unmet": [{"skill": c.label, "demand": c.min_months,
                       "allocated": str(flow.get(("skill", c.key), {}).get(sink, Fraction(0)))}
                      for c in req.clauses
                      if flow.get(("skill", c.key), {}).get(sink, Fraction(0)) < c.min_months],
            "saturated_months": [m for m in ledger.months if ("month", m) not in reach],
            "shared_months": [m for m in ledger.months
                              if sum(1 for k in keys if ledger.supply.get(k, {}).get(m)) >= 2],
            "explanation": (
                f"需求合计 {demand} 个月容量；在每月上限 1 FTE 与各段比例约束下，"
                f"该履历最多可分配 {total}，缺口 {demand - total}，不存在全局可行分配。"),
        }
    return tuple(audits), witness


def evaluate(entries: Iterable[ResumeEntry], requirement: Any, *, contract: str,
             ) -> QualificationReport:
    """只读资格查询：不修改任何采集台账数据，contract 必须显式给出。"""
    if contract not in CONTRACTS:
        raise ValueError(f"contract 必须显式选择 {CONTRACTS} 之一")
    req = parse_requirement(requirement) if isinstance(requirement, str) else requirement
    if not req.clauses:
        raise RequirementError("要求至少包含一个技能子句")
    segments, issues = validate_entries(list(entries))
    ledger = Ledger(segments, issues)
    if contract == "exposure":
        clauses, witness = _eval_exposure(ledger, req), None
    else:
        clauses, witness = _eval_dedicated(ledger, req)
    statuses = {clause.status for clause in clauses}
    verdict = NO_EVIDENCE if NO_EVIDENCE in statuses else (
        UNQUALIFIED if UNMET in statuses else QUALIFIED)
    resume = {"segment_count": len(segments),
              "segments": [{"seg_id": s.seg_id, "label": s.label, "start": s.start.isoformat(),
                            "end": s.end.isoformat(), "skills": list(s.skills),
                            "ratio": str(s.ratio)} for s in segments],
              "month_span": [ledger.months[0], ledger.months[-1]] if ledger.months else None,
              "issue_count": len(ledger.issues)}
    return QualificationReport(contract, verdict, req, clauses,
                               tuple(ledger.issues), witness, resume)


def evaluate_query(payload: dict[str, Any]) -> QualificationReport:
    """从本地合成输入（JSON 字典）执行只读查询；不写入站点内容指纹。"""
    spec = payload.get("requirement")
    req = parse_requirement(spec) if isinstance(spec, str) else requirement_from_dict(spec or {})
    entries = [entry_from_dict(item) for item in payload.get("resume", ())]
    return evaluate(entries, req, contract=str(payload.get("contract", "")))
