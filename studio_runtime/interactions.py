"""Question publication, scoped dependencies and authored handoffs.

A question is not permission to repeat another assignment. Coordination is
part of the responsible parent's ordinary turn, not another model reviewer.
Raw replies and user files never cross topic boundaries through this module.
"""
from copy import deepcopy
import json

from .contracts import digest
from .file_exchange import public

PENDING = {"open", "proposed", "linked"}


def permits_question(q, topic):
    audience = q.get("audience", ["*"])
    return "*" in audience or "parent:" + topic in audience


def directory(state, topic):
    scope = {topic} | {t["id"] for t in state["topics"].values() if t.get("parent") == topic}
    ancestor = state["topics"][topic].get("parent")
    while ancestor:
        scope.add(ancestor)
        ancestor = state["topics"][ancestor].get("parent")
    return [{**{k: deepcopy(q[k]) for k in ("id", "topic_id", "source_job", "state",
                "blocking", "answer_id", "reuse_of", "reviewer") if k in q},
             **({k:deepcopy(q[k]) for k in ("text","why") if k in q} if permits_question(q,topic) else
                {"content_withheld":"source audience does not include this coordinator"})}
            for q in state["questions"].values() if q["topic_id"] in scope or q.get("reviewer") == topic]


def fingerprint(state, topic):
    # Even a plain text arrival (without files or question_id) can make a
    # freshly proposed diagnostic obsolete. Do not silently publish old work.
    return digest({"questions": directory(state, topic),
                   "inputs": [x["id"] for x in state["user_inputs"] if x.get("topic_id") == topic]})


class Interactions:
    def _interaction_context(self, state, topic, role):
        owner = state["topics"][topic].get("parent") or topic
        if role == "parent":
            return {"snapshot": fingerprint(state, topic), "directory": directory(state, topic),
                "review": [deepcopy(q) for q in state["questions"].values()
                           if q.get("reviewer") == topic and q["state"] == "proposed"],
                "handoffs": [deepcopy(h) for h in state.get("question_handoffs", [])
                             if h["coordinator"] == topic and h["state"] not in {"delivered","cancelled"}],
                "boundary": "Question directory is coordination metadata, not replies or attachment reading. Reuse requires a scoped, authored handoff; it does not discharge the child's work."}
        return {"dependencies": [deepcopy(h) for h in state.get("question_handoffs", [])
                                  if h["recipient"] == topic],
                "coordinator": owner,
                "boundary": "Proposed questions await the parent publication decision. Linked replies are not read until a scoped handoff is actually supplied."}

    def _stage_question(self, state, spec, q):
        if state.get("version", 3) < 4:
            return
        topic = spec["metadata"]["topic_id"]
        owner = state["topics"][topic].get("parent")
        task = json.loads(spec["prompt"])["task"]
        if not permits_question(q, owner or topic):
            # The existing audience grant allows this role to ask the human,
            # not to disclose a method-derived question to an unrelated role.
            # Retain that authorized path; do not silently widen the grant.
            q["publication"] = {"source_job":spec["job_id"], "decision":"scope_limited_direct",
                "boundary":"parent content review unavailable under the existing audience restriction"}
            return
        sealed = task.get("interaction", {}).get("snapshot")
        # For old sealed jobs without a snapshot, conservatively review new
        # publications if an existing question/input could have been missed.
        snapshot = spec.get("_publication_snapshot")
        changed = (sealed != snapshot if sealed else bool(
            [x for x in directory(state, topic) if x["id"] != q["id"]]
            or state["user_inputs"]))
        if owner or changed:
            q.update(state="proposed", reviewer=owner or topic,
                     publication_reason="child_coordination" if owner else "interaction_changed_during_call")
            state["topics"][q["reviewer"]]["dirty"] = True
        else:
            q["publication"] = {"source_job":spec["job_id"], "decision":"direct", "snapshot":sealed}

    def _interaction_message(self, state, recipient, text, source_job, *, attachments=(), question_id=None, preserve_inflight=False):
        from .workflow import now
        record = {"id":self._id(state, "INPUT"), "kind":"coordination", "source":"model",
            "topic_id":recipient, "question_id":question_id, "text":text,
            "created_at":now(), "source_job":source_job}
        if attachments:
            record["attachments"] = list(attachments)
        state["user_inputs"].append(record)
        superseded = []
        for job in state["jobs"].values():
            spec = job["spec"]
            if not preserve_inflight and job["state"] == "pending" and spec["role"] in {"author","parent"} and spec["metadata"]["topic_id"] == recipient:
                job["state"] = "superseded"
                superseded.append(spec["job_id"])
                if spec["role"] == "parent":
                    state["topics"][recipient]["dirty"] = True
                    for rid in spec["metadata"].get("request_ids", []):
                        state["inquiries"][rid]["state"] = "ready"
        if superseded:
            state["diagnostics"].append({"kind":"coordination_arrival","input_id":record["id"],
                "superseded_jobs":superseded,"reason":"New scoped dependency/report requires fresh input; sealed prompts and late results remain preserved."})
        topic = state["topics"][recipient]
        topic["state"] = "active"
        topic["dirty" if recipient == "ROOT" else "author_refresh_required"] = True
        # Preserve sealed work. The extra input remains an explicit obligation
        # and a terminal output cannot silently bypass it.
        return record

    def _interaction_decisions(self, con, state, spec, output):
        topic = spec["metadata"]["topic_id"]
        task = json.loads(spec["prompt"])["task"]
        context = task.get("interaction", {})
        visible = {q["id"]:q for q in context.get("directory", [])}
        reviews = {q["id"]:q for q in context.get("review", [])}
        decisions = output.get("question_decisions", [])
        ids = [d["question_id"] for d in decisions]
        if len(set(ids)) != len(ids) or not set(reviews) <= set(ids):
            raise ValueError("question_decisions must handle every visible proposal once")
        for d in decisions:
            qid = d["question_id"]
            if qid not in visible or not d["reason"].strip():
                raise ValueError("question decision needs a visible question and explicit reason")
            q = state["questions"][qid]
            if q["topic_id"] != topic and qid not in reviews and state["topics"][q["topic_id"]].get("parent") != topic:
                raise ValueError("question decision outside coordination scope")
            if d["action"] == "publish":
                if qid not in reviews or q.get("reviewer") != topic:
                    raise ValueError("publish requires a sealed proposal assigned to this parent")
                # Import work but don't publish a user burden against a stale
                # read set. Reconsider once with fresh coordination facts.
                if context.get("snapshot") != spec.get("_publication_snapshot"):
                    state["topics"][topic]["dirty"] = True
                    q.setdefault("history", []).append({**d, "job_id":spec["job_id"], "applied":False,
                        "reason":"coordination changed before publication; reconsider with current facts"})
                    continue
                q.update(state="open", publication={"source_job":spec["job_id"], "decision":"publish",
                    "reason":d["reason"], "snapshot":context["snapshot"]})
            elif d["action"] == "reuse":
                related = d["related_question_id"]
                if related not in visible or related == qid:
                    raise ValueError("reuse must name another actually visible question")
                source = state["questions"][related]
                if (source["state"] not in {"open","answered"} or source["topic_id"] != topic
                        or not permits_question(source,topic) or not permits_question(q,topic)):
                    raise ValueError("reuse requires a parent's active/answered source question; obtain an explicit handoff for other scopes")
                if q["state"] not in PENDING:
                    raise ValueError("cannot reuse a resolved question")
                q.update(state="resolved" if q["topic_id"] == topic else "linked", reuse_of=related, coordinator=topic)
                if q["topic_id"] != topic:
                    self._link_question(state, spec, source, q["topic_id"], d["reason"])
                self._close_question_disposition(con,state,spec,q,"Duplicate request now depends on "+related)
            elif d["action"] == "withdraw":
                if q["state"] in PENDING | {"answered"}:
                    # A received answer stays answered in history even when
                    # the remaining collaboration dependency is withdrawn.
                    if q["state"] in PENDING:
                        q["state"] = "cancelled"
                    q.update(cancellation_reason=d["reason"], cancelled_by=spec["job_id"])
                    for h in state.get("question_handoffs", []):
                        if h["state"] in {"awaiting_answer","awaiting_report"} and (
                                h["question_id"] == qid or h["question_id"] == q.get("reuse_of")
                                and h["recipient"] == q["topic_id"]):
                            h.update(state="cancelled", reason=d["reason"], cancelled_by=spec["job_id"])
                            self._interaction_message(state, h["recipient"],
                                json.dumps({"dependency":h, "decision":"withdraw"}, ensure_ascii=False),spec["job_id"])
                    if q["topic_id"] != topic:
                        self._interaction_message(state, q["topic_id"],
                            json.dumps({"question_id":qid,"decision":"withdraw","reason":d["reason"]}, ensure_ascii=False),
                            spec["job_id"], question_id=qid)
                    self._close_question_disposition(con,state,spec,q,d["reason"])
            elif d["action"] == "escalate":
                parent = state["topics"][topic].get("parent")
                if not parent or qid not in reviews:
                    raise ValueError("only an assigned proposal can escalate to a real ancestor")
                q["reviewer"] = parent
                state["topics"][parent]["dirty"] = True
            else:
                raise ValueError("unknown question decision")
            q.setdefault("history", []).append({**d, "job_id":spec["job_id"], "applied":True})
        # A proposal arriving during the call wasn't in its response scope.
        if any(q["state"] == "proposed" and q.get("reviewer") == topic for q in state["questions"].values()):
            state["topics"][topic]["dirty"] = True

    def _close_question_disposition(self, con, state, spec, question, reason):
        did = question.get("disposition")
        if not did or any(r["disposition"] == did and r["outcome"] == "handled"
                          for r in self.parliament._all(con,"resolution")):
            return
        disposition = self.parliament._get(con,did,"disposition")
        self._op(con,"resolve",id=self._id(state,"RESOLVE"),disposition=did,receiver=disposition["target"],
            outcome="handled",note="Responsible parent closed/superseded this user request in "+spec["job_id"]+
            ". This is not a user answer or proof that its premise is true. "+reason)

    def _link_question(self, state, spec, question, recipient, reason):
        owner = spec["metadata"]["topic_id"]
        if recipient == owner or recipient == "USER" or recipient not in self._scope(state, owner):
            raise ValueError("handoff recipient must be an explicitly named descendant in scope")
        if "*" not in question.get("audience", ["*"]) and "author:" + recipient not in question["audience"]:
            raise ValueError("question source audience does not authorize this handoff recipient")
        links = state.setdefault("question_handoffs", [])
        previous = next((h for h in links if h["question_id"] == question["id"]
                         and h["recipient"] == recipient and h["coordinator"] == owner
                         and h["state"] != "cancelled"), None)
        if previous:
            return previous
        link = {"id":self._id(state,"HANDOFF"), "question_id":question["id"], "recipient":recipient,
            "coordinator":owner, "source_job":spec["job_id"], "state":"awaiting_report" if question["state"] == "answered" else "awaiting_answer",
            "reason":reason, "question":question["text"], "answer_id":question.get("answer_id")}
        links.append(link)
        self._interaction_message(state, recipient, json.dumps({
            "dependency":link, "instruction":"已有用户任务及其答复由父议题协调，避免再次布置同一任务。尚未收到正文，不代表已读取或完成；独立工作可继续。"},
            ensure_ascii=False), spec["job_id"], question_id=question["id"])
        return link

    def _question_handoff_actions(self, con, state, spec, output, candidate_children):
        topic = spec["metadata"]["topic_id"]
        task = json.loads(spec["prompt"])["task"]
        visible = {q["id"] for q in task.get("interaction", {}).get("directory", [])}
        own_new = [q for q in state["questions"].values() if q["source_job"] == spec["job_id"]
                   and "disposition" not in q]
        seen = set()
        for action in output.get("question_handoffs", []):
            qid = action["question_id"]
            if qid.startswith("@self/question/"):
                try:
                    qid = own_new[int(qid.rsplit("/", 1)[-1])]["id"]
                except (IndexError, ValueError):
                    raise ValueError("unknown self question index")
            elif qid not in visible:
                raise ValueError("handoff must name a visible question")
            question = state["questions"][qid]
            if question["topic_id"] != topic:
                raise ValueError("only a question owner can author its answer handoff")
            target = candidate_children.get(action["recipient"], action["recipient"])
            if target not in state["topics"] or (qid,target) in seen or not action["reason"].strip():
                raise ValueError("handoff recipient/reason invalid or repeated")
            seen.add((qid,target))
            if question["state"] not in {"open","answered","proposed"}:
                raise ValueError("handoff source question is no longer active")
            link = self._link_question(state, spec, question, target, action["reason"])
            if not action["summary"].strip():
                if action["attachments"]:
                    raise ValueError("file handoff requires an authored summary")
                continue
            if question["state"] != "answered":
                raise ValueError("an answer handoff requires an actually received answer")
            answer = next(i for i in state["user_inputs"] if i["id"] == question["answer_id"])
            if state.get("input_receipts", {}).get(answer["id"], {}).get("state") != "processed":
                raise ValueError("handoff needs actual source-answer processing, not only the question directory")
            source_versions = []
            for file in answer.get("attachments", []):
                current = state["file_exchange"][link.get("source_version_overrides", {}).get(file["id"], file["id"])]
                seen_versions = set()
                while current["state"] == "superseded" and current.get("superseded_by"):
                    if current["id"] in seen_versions:
                        raise ValueError("cyclic attachment replacement")
                    seen_versions.add(current["id"])
                    current = state["file_exchange"][current["superseded_by"]]
                # FileExchange already enforces full reading for readable
                # sources and an actual failed read for unreadable rejections.
                # Do not require an impossible successful read of a bad file.
                if current["state"] != "handled" or not current.get("uses"):
                    raise ValueError("handoff of a file-based answer requires its file reading/uptake or documented unreadable rejection")
                source_versions.append({"file_id":current["id"],"sha256":current["sha256"],
                    "source_file_id":file["id"],"use":deepcopy(current["uses"][-1])})
            # Never transfer raw user input or user-uploaded files. The parent's
            # explicit report, with traceable source/version, is the boundary.
            originals = []
            for ref in action["attachments"]:
                original = self._resolve_file(con,state,ref,topic,job=spec["job_id"])
                if original["origin"] != "model" or original["topic_id"] != topic:
                    raise ValueError("handoff attachments must be this parent's authored reports, not raw user files")
                if original["id"] in {o["id"] for o in originals}:
                    raise ValueError("handoff file versions must be unique")
                originals.append(original)
            key = digest({"summary":action["summary"], "files":[[f["name"],f["sha256"]] for f in originals],
                          "answer_id":question["answer_id"],
                          "source_versions":[[f["file_id"],f["sha256"]] for f in source_versions]})
            if link.get("report_sha256") == key:
                link.setdefault("duplicate_reports", []).append({"source_job":spec["job_id"],
                    "file_ids":[f["id"] for f in originals], "action":"retained without redelivering identical content"})
                link["state"] = "delivered"
                continue
            record = self._interaction_message(state,target,json.dumps({
                "question_id":qid,"source_answer_id":question["answer_id"],
                "source_file_versions":source_versions,
                "report":action["summary"],"reason":action["reason"],
                "boundary":"父议题撰写的交接报告；不是原始答复或独立验证。附件需读取并明确承接。"},
                ensure_ascii=False),spec["job_id"],question_id=qid)
            bound = [self._copy_handoff_file(con,state,o,target,spec,record) for o in originals]
            if bound:
                record["attachments"] = bound
            link.update(state="delivered",answer_id=question["answer_id"],input_id=record["id"],report_sha256=key,
                        delivered_by=spec["job_id"],source_file_versions=source_versions)
            for q in state["questions"].values():
                if q.get("reuse_of") == qid and q["topic_id"] == target and q["state"] == "linked":
                    q.update(state="resolved",handoff_input=record["id"])
        awaiting = [h for h in state.get("question_handoffs", [])
                    if h["coordinator"] == topic and h["state"] == "awaiting_report"]
        if awaiting and not (output.get("tool_requests") or output.get("inquiry") or output.get("questions")):
            raise ValueError("received answers need an authored handoff or executable preparation; cannot silently strand dependent work")

    def _copy_handoff_file(self, con, state, original, target, spec, record):
        # Independent per-recipient read/uptake receipts; immutable source and
        # lineage retained. No inherited "already read" or "handled" state.
        from .file_exchange import prepare
        import base64
        raw = con.execute("SELECT content FROM artifacts WHERE path=?", (original["path"],)).fetchone()[0].encode()
        files = prepare([{"name":original["name"], "data_base64":base64.b64encode(raw).decode()}])
        marker = {**record, "source":"model_handoff","topic_id":target}
        self._receive_files(con,state,files,marker)
        value = marker["attachments"][0]
        state["file_exchange"][value["id"]]["handoff_source"] = {
            "file_id":original["id"],"sha256":original["sha256"],"source_job":spec["job_id"]}
        return public(state["file_exchange"][value["id"]])

    def _answer_dependencies(self, state, question, record):
        for h in state.get("question_handoffs", []):
            if h["question_id"] == question["id"] and h["state"] != "cancelled":
                h.update(state="awaiting_report", answer_id=record["id"])
                state["topics"][h["coordinator"]]["dirty"] = True

    def _file_dependency_changed(self, state, record):
        replaced = {f.get("replaces") for f in record.get("attachments", [])} - {None, ""}
        if not replaced:
            return
        by_id = {i["id"]:i for i in state["user_inputs"]}
        for h in state.get("question_handoffs", []):
            if h["coordinator"] != record["topic_id"] or h["state"] == "cancelled":
                continue
            q = state["questions"][h["question_id"]]
            source_ids = {f["id"] for f in by_id.get(q.get("answer_id"), {}).get("attachments", [])}
            for incoming in record.get("attachments", []):
                fid, seen = incoming.get("replaces"), set()
                while fid and fid not in seen:
                    seen.add(fid)
                    if fid in source_ids:
                        h.update(state="awaiting_report", changed_by_input=record["id"])
                        h.setdefault("source_version_overrides", {})[fid] = incoming["id"]
                        state["topics"][h["coordinator"]]["dirty"] = True
                        break
                    fid = state["file_exchange"][fid].get("replaces")
