"""Transactional operation lifecycle mixed into Workflow.

All durable data lives in the existing workflow transaction/snapshot. No
parallel hidden agent state, external chat, or replacement supervisor is used.
"""
from copy import deepcopy
import hashlib
import json

from .contracts import digest
from . import cognitive_operations as cognition


class CognitiveWorkflow:
    def cognitive_status_result(self, request):
        with self._tx() as (_,state):
            requester=state["jobs"][request["source_job"]]["spec"]
            if requester["role"] not in {"author","parent"}:
                raise ValueError("cognitive status requires a responsible author")
            jid=request["arguments"].get("job_id")
            job=state["jobs"].get(jid)
            scope=self._scope(state,requester["metadata"]["topic_id"])
            if not job or job["spec"]["metadata"]["topic_id"] not in scope or job["spec"]["role"] not in {"user_review","memory_review","processing"}:
                raise ValueError("job is outside this cognitive status scope")
            return {"outcome":"succeeded","payload":{"job_id":jid,"role":job["spec"]["role"],
                    "state":job["state"],"result_id":job.get("result_id")},
                    "receipt":{"adapter":"workflow_status","boundary":"execution/import only; no private contents or semantic adequacy implied"}}

    def cognitive_result(self, request):
        """Prepare once, wait for real jobs, then expose the exact returned chain."""
        with self._tx() as (con, state):
            original_request = next((r for r in state["tool_requests"] if r["id"] == request["id"]), None)
            if original_request != request or request["kind"] != "cognition.process":
                raise ValueError("cognitive request must match the persisted original")
            plans = state.setdefault("cognitive_operations", {})
            plan = plans.get(request["id"])
            if plan and plan["state"] not in {"returned", "delivered", "consumed"}:
                active = state["jobs"].get(plan.get("active_job"), {})
                if active.get("state") == "superseded":
                    plan["state"] = "superseded"
                    return {"outcome":"failed", "error":"The operation belongs to a superseded topic version. Reframe against current information; any late original is preserved separately.",
                            "receipt":{"adapter":"cognitive_operations", "operation_id":plan["id"], "executed":False}}
            if plan is None:
                spec = state["jobs"][request["source_job"]]["spec"]
                if spec["role"] not in {"parent", "author"} or spec["metadata"]["topic_id"] != request["topic_id"]:
                    raise ValueError("cognitive processing requires a requesting topic author")
                meta=spec["metadata"]
                topic=state["topics"][request["topic_id"]]
                if (meta["revision"] != topic["revision"] or meta["epoch"] != state["epoch"]
                        or meta.get("topic_epoch",0) != topic.get("epoch",0)):
                    raise ValueError("requesting revision changed; retain the original request and reframe explicitly")
                sha = spec["metadata"].get("context_projection", {}).get("source_sha256")
                original = state.get("context_snapshots", {}).get(sha)
                if original is None:
                    # Legacy inputs have only the exact visible task as their scope.
                    original = json.loads(spec["prompt"])["task"]
                    sha = digest(original)
                def resolve_artifact(locator):
                    row = con.execute('SELECT content,source_job FROM artifacts WHERE path=?', (locator['path'],)).fetchone()
                    if not row or row[1] != locator.get('source_job'):
                        raise ValueError('sealed artifact locator no longer matches its exact source')
                    checksum = hashlib.sha256(row[0].encode('utf-8')).hexdigest()
                    if checksum != locator.get('sha256'):
                        raise ValueError('sealed artifact bytes do not match their recorded hash')
                    return row[0], {'path':locator['path'],'source_job':row[1],'sha256':checksum,
                        'start':0,'end':len(row[0]),'total_chars':len(row[0]),'complete':True,
                        'hash_representation':'UTF-8 file bytes','adapter':'sealed_artifact_body',
                        'boundary':'Exact original supplied to this processing call; not yet model uptake or verification.'}
                plan = cognition.prepare(request, original, source_job=spec["job_id"], source_sha=sha, resolve_artifact=resolve_artifact)
                # Build before storing: malformed requests cannot leave half a plan.
                prompt, schema, manifest = cognition.packet(plan)
                plans[plan["id"]] = plan
                self._cognitive_job(state, plan, prompt, schema, manifest)
                return None
            if plan["state"] not in {"returned", "delivered", "consumed"}:
                return None
            return {"outcome": "succeeded", "payload": {
                "operation_id": plan["id"], "operation": plan["operation"],
                "phases": deepcopy(plan["phases"]),
                **({"checklist":deepcopy(plan["checklist"]),
                    "material_manifest":[{"id":m["id"],"source":m["source"],"sha256":digest(m["content"])}
                        for m in plan["sources"]+plan["constraints"]],
                    "review_status":"incomplete" if any(i["status"] == "insufficient_material"
                        for i in plan["phases"][-1]["output"]["items"]) else "reviewed",
                    "semantic_boundary":"reviewed means each checklist item has an authored finding, not independently verified correctness"}
                    if plan["operation"] == "bounded_review" else {}),
                "required_followup": "Explain adoption, preservation or non-adoption in input_uses, then perform an appropriate ordinary action. A received result alone is not a completed cognitive responsibility."},
                "receipt": {"adapter": "cognitive_operations", "source_job": plan["source_job"],
                    "source_sha256": plan["source_sha256"], "actual_result_ids": [p["result_id"] for p in plan["phases"]],
                    "boundary": "Actual model/fixture provenance is preserved per phase. This receipt proves return, not truth, innovation, acceptance or benefit."}}

    def _cognitive_job(self, state, plan, prompt, schema, manifest):
        jid = self._job(state, "processing", plan["topic_id"], prompt,
            {"cognitive_request": plan["id"], "operation_packet": manifest}, schema=schema)
        plan["active_job"] = jid
        plan["state"] = "running"
        return jid

    def _cognitive_compare(self, state, request_id):
        plan = state["cognitive_operations"][request_id]
        if plan["state"] != "compare_pending":
            raise ValueError("comparison no longer awaits a bounded packet")
        prompt, schema, manifest = cognition.packet(plan, "compare")
        self._cognitive_job(state, plan, prompt, schema, manifest)

    def _apply_processing(self, con, state, spec, output, result):
        if "web_request" in spec["metadata"]:
            return self._apply_web(con, state, spec, output, result)
        cognition.validate_response(output, spec)
        plan = state["cognitive_operations"][spec["metadata"]["cognitive_request"]]
        if plan["active_job"] != spec["job_id"] or plan["state"] != "running":
            raise ValueError("operation no longer owns this current result")
        operation = spec["metadata"]["operation_packet"]["operation"]
        plan["phases"].append({"operation": operation, "job_id": spec["job_id"],
            "result_id": result["result_id"], "prompt_sha256": hashlib.sha256(spec["prompt"].encode()).hexdigest(),
            "output_sha256": digest(output), "output": deepcopy(output),
            "evidence_kind": result.get("receipt", {}).get("evidence_kind", "unspecified")})
        if operation == "reinterpret":
            plan["state"] = "compare_pending"
            # Saved before bounded assembly. Overflow retains this actual result
            # and a visible scheduling duty instead of rolling it back.
            self._schedule_intent(state, "cognitive_compare", plan["topic_id"], request_id=plan["id"])
        else:
            plan["state"] = "returned"

    def _cognitive_delivery(self, state, request, record):
        plan = state.get("cognitive_operations", {}).get(request["id"])
        if plan and plan["state"] == "returned":
            plan.update(state="delivered", input_id=record["id"])

    def reframe_cognitive_comparison(self, request_id, *, result_pointers, old_source_ids, reason, source="operator"):
        """Explicit exact-fragment scope after overflow; preserve the full result.

        Does not execute a model or rewrite an old packet. The pending scheduling
        intent will construct a new bounded comparison on the next advance.
        """
        if source not in {"operator", "fixture"} or not isinstance(reason,str) or not reason.strip():
            raise ValueError("comparison scope requires explicit operator/fixture reason")
        with self._tx() as (_,state):
            plan=state.get("cognitive_operations",{}).get(request_id)
            if not plan or plan["state"]!="compare_pending":
                raise ValueError("only a pending comparison can be explicitly reframed")
            if (not isinstance(result_pointers,list) or not result_pointers or any(not isinstance(x,str) for x in result_pointers)
                    or len(set(result_pointers))!=len(result_pointers)
                    or not isinstance(old_source_ids,list) or not old_source_ids or any(not isinstance(x,str) for x in old_source_ids)
                    or len(set(old_source_ids))!=len(old_source_ids)
                    or not set(old_source_ids)<={m["id"] for m in plan["withheld_sources"]}):
                raise ValueError("select nonempty distinct original result pointers and old source ids")
            first=next(p for p in plan["phases"] if p["operation"]=="reinterpret")
            for pointer in result_pointers:
                cognition.pointer_value(first["output"],pointer)
            selection={"result_pointers":deepcopy(result_pointers),"old_source_ids":deepcopy(old_source_ids),
                       "reason":reason,"source":source}
            trial={**deepcopy(plan),"comparison_scope":selection}
            cognition.packet(trial,"compare")
            plan.setdefault("comparison_scope_history",[]).append(selection)
            plan["comparison_scope"]=selection
        return deepcopy(selection)

    def _consume_cognitive_results(self, state, spec, output):
        visible = {i["id"] for i in json.loads(spec["prompt"])["task"].get("user_inputs", [])}
        uses = {i["input_id"]: i for i in output.get("input_uses", [])}
        for plan in state.get("cognitive_operations", {}).values():
            if plan["topic_id"] != spec["metadata"]["topic_id"] or plan["state"] not in {"delivered", "consumed"}:
                continue
            iid = plan["input_id"]
            if plan["state"] == "delivered" and iid in visible and iid not in uses:
                raise ValueError("cognitive result requires explicit adoption, preservation or non-adoption with reason in input_uses: " + iid)
            if iid in uses:
                if not uses[iid]["use"].strip() or not uses[iid]["effect"].strip():
                    raise ValueError("cognitive consumption requires use and effect, including explicit unknown or non-adoption")
                plan["consumption"].append({"job_id": spec["job_id"], **deepcopy(uses[iid]),
                    "followup": {key: deepcopy(output[key]) for key in
                        ("action", "status", "inquiry", "files", "tool_requests", "unresolved") if key in output},
                    "boundary": "author-reported effect, not proof of cognitive benefit"})
                plan["state"] = "consumed"
            if (plan["state"] == "delivered" and
                    (output.get("action") in {"deliver", "quiesce"} or output.get("status") == "complete")):
                raise ValueError("unconsumed cognitive return prevents completion; retrieve and handle its actual result")
