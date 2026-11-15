"""Read-only validation of native-test evidence and additive retry metadata."""
from copy import deepcopy
import hashlib
from pathlib import Path

from .contracts import digest
from .tool_adapters import _reject_links, _json, ToolError, ArtifactReader

def recovery_records(state, topic=None):
    return [x for x in state.get("tool_recoveries", [])
            if topic is None or x["topic_id"] == topic]


def receive_test(state, request, record):
    """Track unavailable execution, not ordinary failing assertions."""
    if request["kind"] != "sandbox.python_tests":
        return
    records = state.setdefault("tool_recoveries", [])
    for duty in records:
        if duty.get("retry_request_id") == request["id"]:
            duty.update(state="resolved", resolution="retry_returned", result_id=record["id"])
    value = record["value"]
    diagnostic = value.get("diagnostic") or (value.get("payload") or {}).get("diagnostic") or {}
    if value.get("outcome") == "uncertain" or (
            value.get("outcome") == "failed" and diagnostic.get("test_code_started") is False):
        records.append({"id":request["id"], "topic_id":request["topic_id"], "source_job":request.get("source_job"),
            "state":"awaiting_action", "result_id":record["id"], "diagnostic":deepcopy(diagnostic),
            "meaning":"Execution needs an explicit operator decision. Waiting is allowed; this is not a test verdict. Retry the preserved files after repair, or dismiss with a recorded reason. Never repeat uncertain execution automatically."})


def retained_test(run_dir, request):
    """No execution or ledger creation. Used before an explicit retry."""
    directory = _reject_links(Path(run_dir) / "tool-executions" / hashlib.sha256(request["id"].encode()).hexdigest())
    sealed = {k:request[k] for k in ("id","kind","tool","arguments","topic_id","source_job","purpose","retry_of","expected_inputs") if k in request}
    request_path = _reject_links(directory / "request.json")
    stored = _json(request_path.read_text(encoding="utf-8")) if request_path.is_file() else None
    if stored is not None and (stored.get("request") != sealed or stored.get("request_sha256") != digest(sealed)):
        raise ToolError("Preserved test request integrity mismatch")
    result_path, receipt_path = (_reject_links(directory / name) for name in ("result.json","receipt.json"))
    if stored is None and (result_path.exists() or receipt_path.exists()):
        raise ToolError("Execution records exist without their sealed request")
    if stored is None or not result_path.is_file() or not receipt_path.is_file():
        value = {"outcome":"uncertain", "error":"Incomplete execution ledger", "payload":None}
    else:
        value, receipt = _json(result_path.read_text(encoding="utf-8")), _json(receipt_path.read_text(encoding="utf-8"))
        if value.get("request_sha256") != digest(sealed) or receipt.get("request_sha256") != digest(sealed):
            raise ToolError("Preserved test result/receipt integrity mismatch")
        if receipt.get("phase") != "completed":
            value = {"outcome":"uncertain", "error":"Incomplete execution commit", "payload":None}
        elif receipt.get("result_sha256") != digest(value):
            raise ToolError("Preserved test result/receipt integrity mismatch")
        value = dict(value, receipt=receipt)
    # Pin exactly the artifact versions that will be retried. Any previous
    # actual reads must match, including failures before the complete copy.
    reader = ArtifactReader(Path(run_dir)/"artifacts", 1024*1024)
    expected = {}
    for item in sealed["arguments"]["files"]:
        _, info = reader.read(item["path"])
        expected[info["path"]] = info["sha256"]
    for item in value.get("receipt",{}).get("reads",[]):
        if expected.get(item["path"]) != item["sha256"]:
            raise ToolError("Original test inputs changed; create a new request instead of retrying this version")
    if sealed.get("expected_inputs") is not None and expected != sealed["expected_inputs"]:
        raise ToolError("Pinned retry inputs changed")
    return value, expected


def test_status(state):
    inputs = {x["id"]:x for x in state.get("user_inputs",[]) if x.get("kind")=="tool_result"}
    receipts = state.get("input_receipts", {})
    rows = []
    for request in state.get("tool_requests",[]):
        if request.get("kind") != "sandbox.python_tests":continue
        entry = inputs.get(request.get("result_id"),{})
        result = entry.get("value",{})
        payload = result.get("payload") or {}
        diagnostic = result.get("diagnostic") or payload.get("diagnostic")
        if not diagnostic and result.get("outcome") in {"failed","uncertain"}:
            from .sandbox_runtime import diagnosis
            diagnostic = diagnosis("legacy_failure",result.get("error") or "Legacy failure without stage information",
                phase="unknown",executed=None)
        rows.append({"id":request["id"],"topic_id":request["topic_id"],"source_job":request.get("source_job"),
            "state":request["state"],"outcome":result.get("outcome"),"error":result.get("error"),
            "diagnostic":diagnostic,"retry_of":request.get("retry_of"),"retried_by":request.get("retried_by"),
            "result_id":entry.get("id"),"uptake":deepcopy(receipts.get(entry.get("id"))),
            "reported_test_count":payload.get("reported_test_count"),"reported_summary":payload.get("reported_summary"),
            "stdout":payload.get("stdout"),"stderr":payload.get("stderr"),
            "receipt_path":result.get("receipt",{}).get("receipt_path"),
            "recovery":next((deepcopy(d) for d in recovery_records(state) if d["id"]==request["id"]),None),
            "retryable":request["state"]=="answered" and result.get("outcome") in {"failed","uncertain"} and not request.get("retried_by")})
    return rows
