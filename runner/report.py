"""Project the raw evidence into a report for regression review."""

import json
from pathlib import Path

from sandbox.storage import write_atomic


def summarize(evidence: dict) -> dict:
    verdict = evidence["verdict"]
    provenance = evidence["provenance"]
    trace = provenance["irisTrace"]
    diagnostics = {key: evidence["errors"][key] for key in ("iris.trace",) if key in evidence["errors"]}
    if trace["possibleEventLimit"]:
        diagnostics["iris.trace.limit"] = "Trace may have reached the Iris event limit."
    if trace["truncatedTextObserved"]:
        diagnostics["iris.trace.text"] = "Iris truncated trace text."
    transcript = evidence["transcript"]
    messages = {row["messageId"]: row for row in transcript}
    monitor = evidence["monitor"]
    snapshot = evidence["sandbox"]
    interactions = [
        {
            "irisReply": messages[decision["messageId"]],
            "recognition": decision["recognition"],
            "action": decision["action"],
            "runnerReply": messages.get(decision["action"].get("messageId")),
        }
        for decision in monitor["decisions"]
    ]
    return {
        "runId": evidence["runId"],
        "scenario": evidence["scenario"],
        "seed": evidence["seed"],
        "irisRevision": provenance.get("irisRevision"),
        "irisWorkingTreeDirty": provenance.get("irisWorkingTreeDirty"),
        "sandboxSource": provenance.get("runnerSource"),
        "irisModels": trace["observedModels"],
        "monitorMode": monitor["mode"],
        "faultPlan": provenance["scenario"].get("faults", []),
        "query": next((row["text"] for row in transcript if row["role"] == "user"), None),
        "interactions": interactions,
        "finalReply": next((row["text"] for row in reversed(transcript) if row["role"] == "iris"), None),
        "orderCount": len(snapshot["state"]["orders"]) if snapshot else None,
        "approvalRequests": len(evidence["approvals"]),
        "approvedRequests": sum(item["approved"] for item in evidence["approvals"]),
        "recovery": evidence.get("recovery"),
        "status": evidence["status"],
        "verdict": verdict["status"],
        "failedChecks": [name for name, passed in verdict["checks"].items() if not passed],
        "errors": {key: value for key, value in evidence["errors"].items() if key != "iris.trace"},
        "diagnostics": diagnostics,
    }


def save_report(path: Path, report: dict) -> None:
    write_atomic(path, (json.dumps(report, ensure_ascii=False, indent=2) + "\n",))
