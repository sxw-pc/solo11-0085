"""Persistence and read-only projections for requirement-impact evidence.

Evidence rows are derived, versioned artifacts.  They never rewrite job
descriptions: a rules-version bump only recomputes this derived layer.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from .requirements import DEFAULT_RULES, Rules, audit_revision

EVIDENCE_COLUMNS = (
    "job_uid", "run_id", "old_run_id", "rules_version", "contract", "change_kind",
    "eligibility_changed", "description_changed", "old_description_sha256",
    "new_description_sha256", "evidence_json", "created_at",
)
EVIDENCE_SQL = f"""INSERT OR REPLACE INTO requirement_evidence(
    {", ".join(EVIDENCE_COLUMNS)}) VALUES ({", ".join("?" for _ in EVIDENCE_COLUMNS)})"""

def evidence_rows(*, job_uid: str, old_run_id: str, run_id: str, old_text: str | None,
                  new_text: str | None, rules: Rules, now: str) -> list[dict[str, Any]]:
    audit = audit_revision(old_text, new_text, rules)
    base = {"job_uid": job_uid, "run_id": run_id, "old_run_id": old_run_id,
            "rules_version": rules.version,
            "description_changed": int((old_text or "") != (new_text or "")),
            "old_description_sha256": audit["text_audit"]["old_sha256"],
            "new_description_sha256": audit["text_audit"]["new_sha256"], "created_at": now}
    rows = []
    for contract, result in (("text_audit", audit["text_audit"]), ("impact", audit["impact"])):
        rows.append({**base, "contract": contract, "change_kind": result["status"],
                     "eligibility_changed": int(result["status"] == "eligibility_changed"),
                     "evidence_json": json.dumps(result, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":"))})
    return rows

def record_evidence(conn: Any, rows: Sequence[dict[str, Any]]) -> None:
    for row in rows:
        conn.execute(EVIDENCE_SQL, tuple(row[column] for column in EVIDENCE_COLUMNS))

def recompute_impact_evidence(store: Any, rules: Rules = DEFAULT_RULES,
                              now: str = "1970-01-01T00:00:00Z", job_uid: str | None = None) -> int:
    """Re-derive evidence from snapshots without touching stored job text."""
    sql, params = "SELECT job_uid, run_id, payload_json FROM job_snapshots", ()
    if job_uid:
        sql, params = sql + " WHERE job_uid = ?", (job_uid,)
    rows = store.conn.execute(sql + " ORDER BY job_uid, observed_at, run_id", params).fetchall()
    previous, changed = {}, 0
    with store.transaction() as conn:
        for row in rows:
            payload = json.loads(row["payload_json"] or "{}")
            description = payload.get("description") or ""
            old = previous.get(row["job_uid"])
            if old is not None and old[1] != description:
                record_evidence(conn, evidence_rows(
                    job_uid=row["job_uid"], old_run_id=old[0], run_id=row["run_id"],
                    old_text=old[1], new_text=description, rules=rules, now=now))
                changed += 1
            previous[row["job_uid"]] = (row["run_id"], description)
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES("
                     "'requirements_rules_version', ?)", (rules.version,))
    return changed

def requirement_report(store: Any, job_uid: str | None = None,
                       rules_version: str | None = None) -> dict[str, Any]:
    empty = {"job_uid": job_uid, "rules_version": rules_version,
             "stored_rules_version": None, "evidence": []}
    if not store.has_table("requirement_evidence"):
        return empty
    rows = store.list_requirement_evidence(job_uid=job_uid, rules_version=rules_version)
    evidence = []
    for row in rows:
        item = {key: row[key] for key in row if key != "evidence_json"}
        item["evidence"] = json.loads(row["evidence_json"])
        evidence.append(item)
    return {"job_uid": job_uid, "rules_version": rules_version,
            "stored_rules_version": store.requirements_rules_version(), "evidence": evidence}
