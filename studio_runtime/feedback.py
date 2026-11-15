"""Finite, revision-bound feedback waves; no polling model or timed debounce."""
from copy import deepcopy


def open_wave(state, parent, source_job, children):
    if not children:
        return
    state["topics"][parent].setdefault("feedback_waves", []).append({
        "id": source_job, "source_job": source_job, "state": "open",
        "expected": {tid: {"revision": state["topics"][tid]["revision"],
                           "epoch": state["topics"][tid].get("epoch", 0)} for tid in children},
        "dispatches": [],
    })


def scope_ids(state, topic):
    result = {topic}
    while True:
        enlarged = result | {t["id"] for t in state["topics"].values() if t.get("parent") in result}
        if enlarged == result:
            return result
        result = enlarged


def feedback_state(state, parent):
    owner = state["topics"][parent]
    rows = []
    for wave in owner.get("feedback_waves", []):
        if wave["state"] == "closed":
            continue
        participants = []
        for tid, version in wave["expected"].items():
            topic = state["topics"].get(tid)
            returns = [rid for rid in (topic or {}).get("awaiting_feedback", [])
                       if state["jobs"].get(state.get("return_sources", {}).get(rid), {}).get("spec", {}).get("metadata", {}).get("revision") == version["revision"]]
            ready = [rid for rid in returns if rid in owner["new_returns"]]
            consumed = [rid for rid in returns if rid in owner["consumed_returns"]]
            detail = []
            if not topic or topic["revision"] != version["revision"] or topic.get("epoch", 0) != version["epoch"]:
                status = "superseded"
            elif ready:
                status = "ready"
            elif any(state.get("return_kinds", {}).get(r, "complete") == "complete" for r in consumed):
                status = "consumed"
            else:
                scope = scope_ids(state, tid)
                failed = [j["spec"]["job_id"] for j in state["jobs"].values()
                          if j["state"] == "failed" and j["spec"]["metadata"]["topic_id"] in scope]
                questions = [q["id"] for q in state["questions"].values()
                             if q["topic_id"] in scope and q["state"] == "open" and q["blocking"]]
                recoveries = [d["id"] for d in state.get("tool_recoveries", [])
                              if d["topic_id"] in scope and d["state"] == "awaiting_action"]
                intents = [i for i in state.get("schedule_intents", []) if i["topic_id"] in scope and i["state"] == "pending"]
                blocked = [d.get("stage") for d in state.get("diagnostics", [])
                           if d.get("status") == "blocked" and (any(
                               d.get("stage", "").endswith(":" + t) for t in scope)
                               or d.get("stage") in {"intent:" + i["id"] for i in intents})]
                if failed or questions or blocked or recoveries:
                    status, detail = "blocked", failed + questions + blocked + recoveries
                elif topic["state"] == "waiting" and not any(
                        j["state"] == "pending" and j["spec"]["metadata"]["topic_id"] in scope for j in state["jobs"].values()):
                    status, detail = "blocked", ["waiting_dependency"]
                else:
                    status = "pending"
            participants.append({"topic": tid, **version, "status": status, "return_ids": ready, "blockers": detail})
        if all(p["status"] in {"consumed", "superseded"} for p in participants):
            wave["state"] = "closed"
        else:
            rows.append({"id": wave["id"], "participants": participants})
    return rows


def parent_ready(state, parent):
    """Only this parent's finite cohort gates its normal feedback turn."""
    owner = state["topics"][parent]
    waves = feedback_state(state, parent)
    participants = [p for w in waves for p in w["participants"]]
    urgent = []
    for item in state["user_inputs"]:
        receipt = state.get("input_receipts", {}).get(item["id"], {})
        already_waiting = (receipt.get("state") == "followup" and receipt.get("job_id")
                           == owner.get("last_decision", {}).get("job_id"))
        if item.get("topic_id") in {None, parent} and receipt.get("state") != "processed" and not already_waiting:
            urgent.append("input:" + item["id"])
    urgent += ["escalation:" + e["disposition_id"] for e in state["escalations"]
               if e["receiver"] == parent and e["state"] == "pending"]
    urgent += ["notice:" + n["source_job"] for n in owner.get("feedback_notices", []) if not n.get("handled")]
    urgent += ["question_review:" + q["id"] for q in state["questions"].values()
               if q.get("reviewer") == parent and q["state"] == "proposed"]
    # A consultation ready for disposition may unblock a branch; do not make
    # it wait for a downstream return that depends on this very disposition.
    urgent += ["inquiry:" + i["id"] for i in state["inquiries"].values()
               if i["topic_id"] == parent and i["state"] == "ready"]
    urgent += ["internal:" + r["id"] for r in state.get("internal_requests", []) if r["owner"] == parent and r["state"] == "open"]
    pending = [p for p in participants if p["status"] == "pending"]
    blocked = [p for p in participants if p["status"] == "blocked"]
    previous = state["jobs"].get(owner.get("last_decision", {}).get("job_id"), {}).get(
        "spec", {}).get("metadata", {}).get("feedback_batch", {})
    def block_key(p):
        return p["topic"], p["revision"], p.get("epoch", 0), tuple(p["blockers"])
    seen_blocks = {block_key(p) for w in previous.get("waves", []) for p in w["participants"] if p["status"] == "blocked"}
    new_block = any(block_key(p) not in seen_blocks for p in blocked)
    reason = "independent_trigger" if urgent else "blocked_partial" if blocked else "cohort_ready" if waves else "no_open_wave"
    ready = not pending or bool(urgent) or new_block
    owner["feedback_wait"] = {"ready": ready, "reason": reason if ready else "awaiting_cohort",
        "urgent": urgent, "waves": waves,
        "boundary": "Blocked and missing returns remain obligations; a partial turn is not permission to complete the task."}
    return ready


def seal_feedback(state, parent, job_id):
    snapshot = deepcopy(state["topics"][parent].get("feedback_wait", {}))
    for wave in state["topics"][parent].get("feedback_waves", []):
        if wave["state"] != "closed":
            wave["dispatches"].append({"job_id": job_id, "state": snapshot.get("reason")})
    return snapshot
