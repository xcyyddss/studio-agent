"""Input lifecycle and durable recovery of attempts and scheduling dependencies."""
from copy import deepcopy
import json
import time
from .input_lifecycle import validate_input_task, validate_input_uses, InputContractError


class Recovery:
    def _refresh_tool_owner(self, state, topic_id, reason):
        """A new execution/version must not be judged by an older sealed call."""
        for job in state["jobs"].values():
            spec = job["spec"]
            if job["state"] in {"pending","failed"} and spec["role"] in {"author","parent"} and spec["metadata"]["topic_id"] == topic_id:
                previous_state = job["state"]
                job["state"] = "superseded"
                for rid in spec["metadata"].get("request_ids",[]):
                    state["inquiries"][rid]["state"] = "ready"
                state["diagnostics"].append({"kind":"tool_input_refresh", "job_id":spec["job_id"],
                    "previous_state":previous_state, "reason":reason})
        topic = state["topics"][topic_id]
        topic["state"] = "active"
        topic["dirty" if topic_id == "ROOT" else "author_refresh_required"] = True
        state["state"] = "active"

    def retry_tool(self, request_id, *, reason, allow_uncertain=False):
        from .tool_recovery import retained_test
        from .workflow import now
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError("A test retry requires a reason of 1–2000 characters")
        with self._tx() as (_, state):
            if not state["metadata"].get("sandbox_tests"):
                raise ValueError("Native Python tests are not enabled for this run")
            old = next((r for r in state["tool_requests"] if r["id"]==request_id),None)
            if not old or old["kind"]!="sandbox.python_tests" or old["state"]!="answered":
                raise ValueError("Only a returned Python test request can be retried")
            if old.get("retried_by"):
                return {"previous_request_id":request_id,"new_request_id":old["retried_by"],"already_created":True}
            result, expected = retained_test(self.path,old)
            saved = next((x for x in state["user_inputs"] if x["id"]==old.get("result_id")),None)
            if not saved or saved["value"].get("outcome") != result.get("outcome"):
                raise ValueError("Workflow and preserved test result disagree")
            if result["outcome"] not in {"failed","uncertain"}:
                raise ValueError("Successful tests cannot be retried through failure recovery")
            if result["outcome"]=="uncertain" and allow_uncertain is not True:
                raise ValueError("Test execution is uncertain; explicit allow_uncertain is required")
            topic = state["topics"][old["topic_id"]]
            if topic["state"]=="retired":raise ValueError("The original topic is retired; a current owner must plan new work")
            new = {k:deepcopy(old[k]) for k in ("kind","arguments","topic_id","source_job","purpose")}
            new.update(id=self._id(state,"TOOL"),state="requested",retry_of=request_id,
                expected_inputs=expected,created_at=now(),retry_reason=reason,allow_uncertain=bool(allow_uncertain))
            state["tool_requests"].append(new)
            old["retried_by"] = new["id"]
            for duty in state.get("tool_recoveries", []):
                if duty["id"] == request_id:
                    duty.update(state="retry_requested", retry_request_id=new["id"])
            self._refresh_tool_owner(state,old["topic_id"],"Explicit retry "+new["id"]+" of "+request_id)
            topic["dirty" if old["topic_id"] == "ROOT" else "author_refresh_required"] = False
            # Do not dispatch a model merely to reissue the tool. Existing
            # scheduling waits on the requested execution and its real return.
            return {"previous_request_id":request_id,"new_request_id":new["id"]}

    def dismiss_tool_recovery(self, request_id, *, reason):
        from .workflow import now
        if not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError("Dismissing recovery requires a reason of 1–2000 characters")
        with self._tx() as (_, state):
            duty = next((d for d in state.get("tool_recoveries", []) if d["id"]==request_id),None)
            if not duty or duty["state"] != "awaiting_action":
                raise ValueError("Only an execution awaiting operator action can be dismissed")
            duty.update(state="dismissed", reason=reason, resolved_at=now())
            record = {"id":self._id(state,"INPUT"), "kind":"tool_recovery_decision", "source":"operator",
                "topic_id":duty["topic_id"], "request_id":request_id,
                "text":"Operator ended this execution recovery: "+reason+". This is not a successful test result.",
                "created_at":now()}
            state["user_inputs"].append(record)
            duty["decision_input_id"] = record["id"]
            self._refresh_tool_owner(state,duty["topic_id"],"Explicit execution recovery decision")
            return record

    def _input_obligations(self, state, task):
        receipts = state.get("input_receipts", {})
        return [x["id"] for x in task.get("user_inputs", []) if x.get("id")
                and receipts.get(x["id"], {}).get("state") != "processed"]

    def _acknowledge_inputs(self, state, spec, output):
        task = json.loads(spec["prompt"])["task"]
        validate_input_uses(task, output)
        uses = output.get("input_uses", [])
        if any(u.get("disposition") == "followup" for u in uses) and (output.get("action") in {"deliver","quiesce"} or output.get("status") == "complete"):
            raise ValueError("unprocessed input follow-up prevents terminal completion")
        existing_dependency = any(item.get("state") in states for field, states in (
            ("tool_requests", {"requested"}), ("tool_recoveries", {"awaiting_action","retry_requested"}),
            ("questions", {"open","proposed","linked"}), ("escalations", {"pending", "deferred"}))
            for item in task.get(field, []))
        existing_dependency = existing_dependency or any(h["state"] in {"awaiting_answer","awaiting_report"}
            for h in task.get("interaction", {}).get("dependencies", []))
        if any(u.get("disposition") == "followup" for u in uses) and not any(
                output.get(k) for k in ("questions","tool_requests","inquiry","unresolved")) and not (
                existing_dependency and (output.get("action") in {"wait","checkpoint"} or output.get("status") in {"wait","checkpoint"})):
            raise ValueError("deferred input needs an executable follow-up or an explicit unresolved obligation")
        for use in uses:
            if not use["use"].strip() or not use["effect"].strip():
                raise ValueError("input acknowledgment requires use and effect, including unknown effect")
            record = state.setdefault("input_receipts", {}).setdefault(use["input_id"], {"history":[]})
            if (output.get("progress", {}).get("kind") == "no_change" and record.get("state") == "processed"
                    and use["disposition"] == "followup"):
                raise ValueError("reopening a processed input is new work, not no_change")
            record["history"].append({"job_id":spec["job_id"], **deepcopy(use)})
            record.update(state=use["disposition"], job_id=spec["job_id"], effect=use["effect"], use=use["use"])

    @staticmethod
    def _input_packet_error(spec):
        if spec["role"] not in {"parent", "author"}:
            return None
        try:
            validate_input_task(json.loads(spec["prompt"])["task"])
        except InputContractError as exc:
            return str(exc)
        return None

    def _input_status(self, state, *, compact=False):
        result = []
        jobs_by_input, preparations = {}, {}
        for job in state['jobs'].values():
            if job['spec']['role'] in {'author','parent','user_review'}:
                for iid in job['spec']['metadata'].get('input_ids',[]):
                    jobs_by_input.setdefault(iid,[]).append(job)
        for unit in state.get('context_memory',{}).get('units',{}).values():
            if unit['state'] in {'summarizing','reviewing'} and unit.get('active_job') in state['jobs']:
                preparations.setdefault(unit['topic_id'],[]).append(state['jobs'][unit['active_job']])
        internal = {r['input_id']:r for r in state.get('internal_requests',[]) if r.get('input_id')}
        for original in state["user_inputs"]:
            if original.get("kind") == "tool_result":
                continue
            iid, topic = original["id"], original.get("topic_id", "ROOT")
            receipt = state.get("input_receipts", {}).get(iid, {})
            jobs = jobs_by_input.get(iid,[])
            pending = [j for j in jobs if j["state"] == "pending"]
            failed = [j for j in jobs if j["state"] == "failed"]
            preparing = preparations.get(topic,[])
            failed += [j for j in preparing if j["state"] == "failed"]
            lifecycle = ("processed" if receipt.get("state") == "processed" else
                         "processing" if pending else "failed" if failed else
                         "preparing" if any(j["state"] == "pending" for j in preparing) else
                         "followup" if receipt.get("state") == "followup" else "received")
            if compact and original.get('kind') == 'internal_response':
                # The exact file bodies remain in the immutable input and are
                # delivered to its recipient, not re-sent on every UI poll.
                response = internal.get(iid,{}).get('response',{})
                display_text = response.get('report','内部交付，正文保存在原始输入中')
                if response.get('artifacts'): display_text += '\n文件：' + '；'.join(response['artifacts'])
            else:
                display_text = original.get('text','')
            result.append({"input_id":iid,"question_id":original.get("question_id"),"topic_id":topic,
                "text":display_text,"kind":original.get("kind"),"source":original.get("source"),
                "state":lifecycle,"effect":receipt.get("effect"),
                "use":receipt.get("use"),"job_id":receipt.get("job_id"),
                "pending_jobs":[j["spec"]["job_id"] for j in pending],
                "failed_jobs":[{"job_id":j["spec"]["job_id"],"error":j.get("error"),
                                "retry_at":j.get("retry_at")} for j in failed]})
        return result

    def _replace_active_job(self, state, old, new):
        """Rebind active control references, never provenance/history references."""
        for inquiry in state["inquiries"].values():
            inquiry["jobs"] = [new if j == old else j for j in inquiry["jobs"]]
        for repair in state.get("reassessments", {}).values():
            repair["jobs"] = [new if j == old else j for j in repair["jobs"]]
        for delivery in state.get("evidence_deliveries", []):
            for batch in delivery.get("batches", []):
                if batch.get("job_id") == old:
                    batch.setdefault("previous_jobs", []).append(old)
                    batch["job_id"] = new
        for collection in (state.get("cognitive_operations", {}), state.get("web_research", {}),
                           state.get("context_memory", {}).get("units", {})):
            for entry in collection.values():
                if entry.get("active_job") == old:
                    entry["active_job"] = new

    def _request_capacity_rebuild(self, state, job_id):
        job = state["jobs"][job_id]
        meta = job["spec"]["metadata"]
        topic = state["topics"][meta["topic_id"]]
        from .context_lifecycle import capacity, policy
        measured = capacity(job["spec"]["prompt"], job["spec"]["schema"], settings=policy(state))
        error = job.get("error") or {}
        char_bound = error.get("boundary") == "cli_input_chars" or not measured["character_fits"]
        topic["context_pressure"] = {"job_id":job_id,"error":error,
            **({"boundary":"cli_input_chars", "max_prompt_chars": min(
                error.get("max_chars", measured["max_prompt_chars"]), measured["max_prompt_chars"])}
               if char_bound else {"target_text_tokens": max(1, measured["accounted_text_tokens"] * 3 // 4)})}
        topic.setdefault("capacity_replacement", {})[job["spec"]["role"]] = job_id
        job["state"] = "superseded"
        job.pop("retry_at", None)
        if job["spec"]["role"] == "parent":
            topic["dirty"] = True
            for rid in meta.get("request_ids", []):
                state["inquiries"][rid]["state"] = "ready"
        else:
            topic["author_refresh_required"] = True

    def recover_failures(self):
        """Back off known transient failures; never replay uncertain execution."""
        eligible = []
        with self._tx() as (_, state):
            if state["paused"]:
                return []
            self._recognize_capacity_failures(state)
            for jid, job in state["jobs"].items():
                if (job["state"] == "failed" and job.get("failure_kind") == "context_capacity"
                        and job["spec"]["role"] in {"author","parent"}):
                    meta = job["spec"]["metadata"]
                    topic = state["topics"][meta["topic_id"]]
                    if not topic.get("context_pressure"):
                        self._request_capacity_rebuild(state, jid)
                    continue
                if job["state"] != "failed" or job.get("failure_kind") != "transport" or job.get("uncertain"):
                    continue
                count = job["spec"]["metadata"].get("transport_retries", 0)
                if count >= 3:
                    job.pop("retry_at", None)
                    continue
                job.setdefault("retry_at", time.time() + (5, 15, 45)[count])
                if time.time() >= job["retry_at"]:
                    eligible.append(jid)
        recovered = []
        for jid in eligible:
            new = self.retry(jid)
            with self._tx() as (_, state):
                state["jobs"][new]["spec"]["metadata"]["transport_retries"] = state["jobs"][jid]["spec"]["metadata"].get("transport_retries", 0) + 1
            recovered.append(new)
        return recovered

    def _recognize_capacity_failures(self, state):
        """Add a diagnostic to legacy failures without editing their raw result.

        The saved prompt itself proves that it cannot pass the host boundary.
        No speculative stderr path (often absolute on another machine) is read.
        """
        from .context_budget import CLI_MAX_INPUT_CHARS
        for jid, job in state["jobs"].items():
            if (job["state"] != "failed" or job.get("uncertain")
                    or job.get("failure_kind") not in {None, "unknown", "context_capacity"}
                    or job["spec"]["role"] not in {"author", "parent"}
                    or len(job["spec"]["prompt"]) <= CLI_MAX_INPUT_CHARS):
                continue
            if (job.get("error") or {}).get("boundary") == "cli_input_chars":
                continue
            original = deepcopy(job.get("error"))
            job.update(failure_kind="context_capacity", error={
                **(original or {}), "category":"context_capacity", "boundary":"cli_input_chars",
                "max_chars":CLI_MAX_INPUT_CHARS, "actual_chars":len(job["spec"]["prompt"]),
                "message":"保存的调用输入超过 CLI 字符上限，需重新组装；历史原始结果保留。", "retryable":False})
            state["diagnostics"].append({"kind":"capacity_failure_reclassified", "job_id":jid,
                "original_error":original, "evidence":"immutable saved prompt length",
                "actual_chars":len(job["spec"]["prompt"]), "max_chars":CLI_MAX_INPUT_CHARS})

    def recover_scheduling(self):
        """Public retry for pre-job blockers, after policy/configuration repair."""
        with self._tx() as (con, state):
            state["paused"] = False
            self._recognize_capacity_failures(state)
            for jid, job in state["jobs"].items():
                if job["state"] == "failed" and job.get("failure_kind") == "context_capacity" and job["spec"]["role"] in {"parent","author"}:
                    self._request_capacity_rebuild(state, jid)
            for d in state["diagnostics"]:
                if d.get("status") == "blocked":
                    d["status"] = "retry_requested"
            self._progress(con, state)
        return self.status()
