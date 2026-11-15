"""Native research as durable jobs and scoped, explicitly consumed tool results."""
from copy import deepcopy
import json

from .contracts import digest, canonical
from . import web_research


class WebWorkflow:
    def web_result(self, request, *, mode="live"):
        with self._tx() as (con, state):
            original = next((r for r in state["tool_requests"] if r["id"] == request["id"]), None)
            if original != request or request["kind"] != "web.search":
                raise ValueError("search request must match the persisted original")
            plans = state.setdefault("web_research", {})
            plan = plans.get(request["id"])
            if plan is None:
                spec = state["jobs"][request["source_job"]]["spec"]
                if spec["role"] not in {"author", "parent"} or spec["metadata"]["topic_id"] != request["topic_id"]:
                    raise ValueError("search requires a requesting topic author")
                meta, current = spec["metadata"], state["topics"][request["topic_id"]]
                if (meta["epoch"] != state["epoch"] or meta["revision"] != current["revision"] or
                        meta.get("topic_epoch", 0) != current.get("epoch", 0)):
                    raise ValueError("search belongs to a changed topic version; reframe the request")
                if mode == "disabled":
                    return {"outcome": "failed", "error": "Native search is disabled for this run; no call was made.",
                            "receipt": {"adapter": "codex_native_web", "executed": False}}
                prompt, schema, policy, accounting = web_research.packet(request, mode)
                jid = self._job(state, "processing", request["topic_id"], prompt,
                    {"web_request": request["id"], "native_web": policy,
                     "operation_packet": {"operation": "web_research", "accounting": accounting}}, schema=schema)
                plan = {"id": request["id"], "topic_id": request["topic_id"], "source_job": request["source_job"],
                        "state": "running", "active_job": jid, "request": deepcopy(request), "consumption": []}
                plans[plan["id"]] = plan
                return None
            if plan["state"] in {"returned", "delivered", "consumed"}:
                return deepcopy(plan["result"])
            job = state["jobs"].get(plan["active_job"], {})
            if job.get("state") == "superseded":
                plan["state"] = "superseded"
                return {"outcome": "failed", "error": "Search revision superseded; late evidence is preserved without changing the current topic.",
                        "receipt": {"adapter": "codex_native_web", "executed": False}}
            # Failed/uncertain jobs remain inspectable and use the standard retry
            # mechanism. They must not become automatic duplicate paid calls.
            return None

    def _apply_web(self, con, state, spec, output, result):
        plan = state["web_research"][spec["metadata"]["web_request"]]
        if plan["active_job"] != spec["job_id"] or plan["state"] != "running":
            raise ValueError("search job no longer owns this request")
        receipt = result.get("receipt", {})
        capture = receipt.get("native_web", {})
        if not capture or receipt.get("native_web_sha256") != digest(capture):
            raise ValueError("native search result lacks its immutable event capture")
        if capture.get("mode") != spec["metadata"]["native_web"]["mode"]:
            raise ValueError("native search mode differs from sealed request")
        plan["capture"] = deepcopy(capture)
        plan["result"] = web_research.evidence(output, capture, request=plan["request"], job_id=spec["job_id"],
                            result_id=result["result_id"], evidence_kind=receipt.get("evidence_kind", "unspecified"))
        plan["result_id"] = result["result_id"]
        plan["state"] = "returned"

    def web_read(self, request):
        """Read exact captured events in bounded ranges, without another search."""
        with self._tx() as (_, state):
            original = next((r for r in state["tool_requests"] if r["id"] == request["id"]), None)
            if original != request or request["kind"] != "web.read":
                raise ValueError("web.read must match an original request")
            args = request["arguments"]
            if not {"request_id"} <= set(args) <= {"request_id", "start", "max_chars"}:
                raise ValueError("web.read requires request_id, optional start and max_chars")
            plan = state.get("web_research", {}).get(args["request_id"])
            if not plan or plan["topic_id"] not in self._scope(state, request["topic_id"]) or "capture" not in plan:
                raise ValueError("search original is unavailable in this responsibility scope")
            start, count = args.get("start", 0), args.get("max_chars", 4000)
            if type(start) is not int or start < 0 or type(count) is not int or not 1 <= count <= 8000:
                raise ValueError("invalid original range")
            text = canonical(plan["capture"])
            if start > len(text):
                raise ValueError("range starts beyond original")
            return {"outcome": "succeeded", "payload": {"request_id": plan["id"], "source_job": plan["active_job"],
                "source_result": plan["result_id"], "sha256": digest(plan["capture"]), "start": start,
                "end": min(start + count, len(text)), "total_chars": len(text), "content": text[start:start + count]},
                "receipt": {"adapter": "native_web_original", "new_search": False}}

    def _web_delivery(self, state, request, record):
        plan = state.get("web_research", {}).get(request["id"])
        if plan and plan["state"] == "returned":
            plan.update(state="delivered", input_id=record["id"])

    def _consume_web(self, state, spec, output):
        visible = {i["id"] for i in json.loads(spec["prompt"])["task"].get("user_inputs", [])}
        uses = {i["input_id"]: i for i in output.get("input_uses", [])}
        for plan in state.get("web_research", {}).values():
            if plan["topic_id"] != spec["metadata"]["topic_id"] or plan["state"] not in {"delivered", "consumed"}:
                continue
            iid = plan["input_id"]
            if plan["state"] == "delivered" and iid in visible and iid not in uses:
                raise ValueError("search result requires explicit use/non-use and effect in input_uses: " + iid)
            if iid in uses:
                if not uses[iid]["use"].strip() or not uses[iid]["effect"].strip():
                    raise ValueError("search result consumption requires use and effect")
                plan["consumption"].append({"job_id": spec["job_id"], **deepcopy(uses[iid]),
                    "followup": {key: deepcopy(output[key]) for key in ("action", "status", "inquiry", "files", "tool_requests") if key in output}})
                plan["state"] = "consumed"
            if plan["state"] == "delivered" and (output.get("action") in {"deliver", "quiesce"} or output.get("status") == "complete"):
                raise ValueError("unhandled search return prevents completion; retrieve and address the result")
