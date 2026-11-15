"""Lossless audit storage, compact projection of explicitly unchanged work.

No semantic similarity guess deletes an advance. Full accepted output remains
in results; the same scoped role can retrieve it, including omitted no-change
reports. This projection runs without a model or a summary/review job.
"""
from copy import deepcopy
import json

from .contracts import digest


def validate_progress(output):
    progress = output.get("progress", {})
    if progress.get("kind") != "no_change":
        return
    if not progress.get("reason", "").strip():
        raise ValueError("no_change requires an explicit reason")
    if output.get("status", output.get("action")) != "wait":
        raise ValueError("no_change can only retain an actual dependency wait")
    actions = ("files", "questions", "tool_requests", "unresolved", "contributions",
               "dispositions", "contribution_decisions", "selection_responses",
               "escalation_responses", "question_decisions", "question_handoffs", "inquiry_uses",
               "internal_requests", "internal_responses", "work_updates", "obligation_decisions", "stage_artifacts")
    if any(output.get(key) for key in actions) or output.get("inquiry") or output.get('phase',{}).get('name'):
        raise ValueError("new work/decisions must be recorded as advance, not no_change")
    if output.get("parent_notice", {}).get("priority") == "urgent":
        raise ValueError("an urgent notice is an advance")


def project(records):
    result = []
    for record in records:
        output = record["output"]
        unchanged = output.get("progress", {}).get("kind") == "no_change"
        signature = record.get('original', {}).get('sha256') or digest(output)
        prior = result[-1] if result else None
        # Exact duplicates are also transport-deduplicated; all unique advances
        # retain full bodies, even if they look similar to an earlier conclusion.
        if (prior and prior["revision"] == record["revision"] and
                (unchanged or signature == prior.get("output_sha256"))):
            repeats = prior.setdefault("following_receipts", {"count":0,
                "first_job":record["job_id"], "last_job":record["job_id"],
                "retrieval":"history.read with job_id; history.catalog lists exact archived IDs",
                "boundary":"Unchanged is the author's declaration, not verified semantic equivalence. Full originals remain stored."})
            repeats["count"] += 1
            repeats["last_job"] = record["job_id"]
        else:
            result.append({**deepcopy(record), "output_sha256":signature})
    return result


class WorkHistory:
    def history_result(self, request):
        with self._tx() as (con, state):
            saved = next((r for r in state["tool_requests"] if r["id"] == request["id"]), None)
            if saved != request:
                raise ValueError("history request must match the persisted original")
            spec = state["jobs"][request["source_job"]]["spec"]
            role, topic = spec["role"], spec["metadata"]["topic_id"]
            if role not in {"author", "parent"}:
                raise ValueError("history requires a responsible role")
            args = request["arguments"]
            records = self._work_history(con, state, topic, role, archived=True)
            if request["kind"] == "history.catalog":
                cursor = args.get("cursor", 0)
                if type(cursor) is not int or cursor < 0:
                    raise ValueError("history cursor must be nonnegative")
                payload = {"entries":[{k:r[k] for k in ("job_id","result_id","revision","created_at")}
                                      for r in records[cursor:cursor+20]],
                           "next_cursor":cursor+20 if len(records)>cursor+20 else None}
            elif request["kind"] == "history.read":
                record = next((r for r in records if r["job_id"] == args.get("job_id")), None)
                if record is None:
                    raise ValueError("history job is outside this exact role/topic or not applied")
                body = json.dumps(record, ensure_ascii=False, sort_keys=True)
                start, size = args.get("start",0), args.get("max_chars",16000)
                if type(start) is not int or type(size) is not int or not 0 <= start <= len(body) or not 1 <= size <= 32000:
                    raise ValueError("invalid history range")
                payload = {"job_id":record["job_id"],"sha256":digest(record),"start":start,
                    "end":min(start+size,len(body)),"total_chars":len(body),"content":body[start:start+size]}
            else:
                raise ValueError("unknown history operation")
            return {"outcome":"succeeded","payload":payload,"receipt":{"adapter":"scoped_history",
                "boundary":"Exact archived role output, not newly verified facts or a new contribution."}}
