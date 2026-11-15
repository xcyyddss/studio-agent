"""Durable workflow for independently evaluated council and author outputs.

Council transitions, workflow state and job intents share one transaction.
Process execution and external actions are handled by separate services.
"""
from __future__ import annotations

from contextlib import contextmanager, closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path, PurePosixPath
import sqlite3
import uuid

from studio_runtime.parliament import Parliament, ParliamentError, DEFAULT_LIMITS, encode
from .context import select_input, evidence_batches, ordinary_input, PROTOCOL, ProjectionBudgetError
from studio_runtime.selection import SelectionBudgetError
from .roles import SCHEMAS, validate, runtime_schema
from .cognitive_protocols import attach, inspect_prompt
from .cognitive_workflow import CognitiveWorkflow
from .web_workflow import WebWorkflow
from .file_exchange import FileExchange
from .context_lifecycle import ContextLifecycle, ContextPending, policy as context_policy, capacity
from .recovery import Recovery
from .interactions import Interactions, PENDING as PENDING_QUESTIONS, fingerprint as interaction_fingerprint
from .input_lifecycle import validate_input_task, InputContractError
from .state_store import StateStore
from .dependencies import Dependencies
from .work_state import WorkState, working_history
from .work_history import WorkHistory, project as project_history, validate_progress


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def _result_source_kind(result):
    """Fixture receipts are synthetic evidence regardless of their field format."""
    receipt = result.get("receipt") or {}
    return "fixture" if receipt.get("evidence_kind") == "fixture" or receipt.get("provider") == "fixture" else "model"


class WorkflowError(ValueError):
    pass


class _RunParliament(Parliament):
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.db = self.path / "workflow.db"


class Workflow(WorkState, Dependencies, WorkHistory, Interactions, Recovery, ContextLifecycle, CognitiveWorkflow, WebWorkflow, FileExchange):
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.db = self.path / "workflow.db"
        if not self.db.is_file():
            raise WorkflowError("workflow is not initialized")
        self.parliament = _RunParliament(self.path)

    @classmethod
    def create(cls, path, goal, seed_materials=None, *, metadata=None, members=None, run_id=None, version=4, profile_seeds=None, council_limits=None):
        if not isinstance(goal, str) or not goal.strip():
            raise WorkflowError("a nonempty ordinary goal is required")
        chosen_limits = dict(DEFAULT_LIMITS if council_limits is None else council_limits)
        if set(chosen_limits) != set(DEFAULT_LIMITS) or any(type(v) is not int or v <= 0 for v in chosen_limits.values()):
            raise WorkflowError("council limits require abstract_chars, scenario_chars and total_chars as positive integers")
        directory = Path(path).resolve()
        created_directory = not directory.exists()
        directory.mkdir(parents=True, exist_ok=True)
        db = directory / "workflow.db"
        if db.exists():
            raise WorkflowError("run already exists; open it to continue")
        with closing(sqlite3.connect(db)) as con, con:
            con.execute("PRAGMA journal_mode=WAL")
            StateStore.initialize(con)
            con.executescript("""
                CREATE TABLE objects(id TEXT PRIMARY KEY, kind TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE heads(topic TEXT PRIMARY KEY, revision TEXT NOT NULL);
                CREATE TABLE events(seq INTEGER PRIMARY KEY, time TEXT NOT NULL, op TEXT NOT NULL, body TEXT NOT NULL);
                CREATE TABLE workflow(key TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE results(result_id TEXT PRIMARY KEY, body TEXT NOT NULL, outcome TEXT NOT NULL);
                CREATE TABLE artifacts(path TEXT PRIMARY KEY, content TEXT NOT NULL, source_job TEXT NOT NULL);
            """)
            con.execute("INSERT INTO objects VALUES(?,?,?)", ("CONFIG", "config", encode({"schema": 1, "limits": chosen_limits})))
            state = {"schema": "studio_workflow.v4" if version >= 4 else "studio_workflow.v3", "version": version, "run_id": run_id or directory.name,
                     "goal": goal, "metadata": metadata or {}, "created_at": now(), "epoch": 0, "sequence": 0,
                     "state": "active", "state_reason": "ordinary goal initialized", "paused": False,
                     "members": members or [{"id": "M.COGNITION", "perspective": "问题边界、认识修正和抽象关联"},
                                            {"id": "M.PRACTICE", "perspective": "可检查产物、工程实践和反馈条件"}],
                     "topics": {}, "inquiries": {}, "jobs": {}, "questions": {}, "user_inputs": [],
                     "deliveries": [], "obligations": [], "tool_requests": [], "selections": [], "late_results": [], "escalations": []}
            con.execute("INSERT INTO workflow VALUES('state',?)", (encode(state),))
        self = cls(directory)
        with self._initial_tx(created_directory) as (con, state):
            self._op(con, "root", id="ROOT", q=goal, actor="ROOT.AUTHOR")
            state["topics"]["ROOT"] = {"id": "ROOT", "parent": None, "revision": "ROOT.v0", "state": "active",
                                          "summary": "", "new_returns": [], "consumed_returns": [], "dirty": False}
            if version >= 4:
                from . import user_collaboration as user
                user.initialize(state, goal, profile_seeds)
                self.parliament._put(con, "USER", "topic", parent="ROOT", actor="USER.AUTHOR", fixed=True)
                self.parliament._put(con, "USER.v0", "revision", topic="USER", parent="ROOT", questions=["理解用户并改善当前目标中的协作"],
                    I=[], candidates=[], previous=None, continuity="fresh", unresolved_previous="", relation="fixed_user_collaboration")
                con.execute("INSERT INTO heads VALUES('USER','USER.v0')")
                state["topics"]["USER"] = {"id": "USER", "parent": "ROOT", "revision": "USER.v0", "state": "sleeping",
                    "summary": "固定用户协作职责；无新证据时休眠。", "new_returns": [], "consumed_returns": [], "dirty": False, "fixed": True}
                con.execute("INSERT INTO events(time,op,body) VALUES(?,?,?)", (now(), "fixed_user_topic", encode({"source": "fixed_user_collaboration", "parent": "ROOT"})))
            seeds = [] if seed_materials is None else seed_materials
            for member in state["members"]:
                self._op(con, "member", **member)
                self._op(con, "material", id=member["id"] + ".PROTOCOL", owner=member["id"], content=PROTOCOL,
                         window="abstract", source={"kind": "observation", "reference": "studio_runtime/context.py#PROTOCOL"})
                for n, seed in enumerate(seeds):
                    if seed.get("purpose", "design_association") not in {"design_association", "task_material"}:
                        raise WorkflowError("seed purpose is not authorized for this ordinary task")
                    self._op(con, "material", id=member["id"] + f".SEED{n + 1}", owner=member["id"],
                             content=seed["content"], window=seed.get("window", "scenario"), source=seed["source"])
            self._inquiry(con, state, "ROOT", goal)
            if version >= 4:
                self._user_review(con, state)
        self._export()
        return self

    @classmethod
    def open(cls, path):
        self = cls(path)
        self._export()
        return self

    @contextmanager
    def _initial_tx(self, created_directory):
        try:
            with self._tx() as pair:
                yield pair
        except BaseException:
            # This file was created exclusively for this initialization, and no
            # role intent was published. Retain any unrelated preexisting files.
            self.db.unlink(missing_ok=True)
            if created_directory:
                try:
                    self.path.rmdir()
                except OSError:
                    pass
            raise

    @contextmanager
    def _tx(self):
        con = sqlite3.connect(self.db, timeout=30)
        try:
            con.execute("BEGIN IMMEDIATE")
            StateStore.initialize(con)
            store = StateStore(self.db, con)
            state = store.load()
            state.setdefault("escalations", [])
            state.setdefault("evidence_deliveries", [])
            state.setdefault("return_sources", {})
            state.setdefault("reassessments", {})
            state.setdefault("context_repairs", [])
            state.setdefault("assessment_sources", {})
            state.setdefault("parent_contribution_repairs", [])
            state.setdefault("diagnostics", [])
            state.setdefault("context_snapshots", {})
            state.setdefault("obligation_catalogs", {})
            state.setdefault("memory_reviews", {})
            state.setdefault("schedule_intents", [])
            state.setdefault("input_receipts", {})
            yield con, state
            store.save(state)
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    @contextmanager
    def _read_tx(self):
        con = sqlite3.connect(self.db.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            state = StateStore(self.db, con).load()
            # Old runs may lack optional collection keys; this is an in-memory
            # view only, never a migration or write from an HTTP status query.
            for key in ("schedule_intents", "diagnostics", "evidence_deliveries", "context_repairs", "parent_contribution_repairs", "escalations"):
                state.setdefault(key, [])
            yield con, state
        finally:
            con.rollback()
            con.close()

    def _id(self, state, prefix):
        state["sequence"] += 1
        return f"{prefix}.{state['sequence']}"

    def _op(self, con, op, **fields):
        result = getattr(self.parliament, "_op_" + op)(con, **fields)
        con.execute("INSERT INTO events(time,op,body) VALUES(?,?,?)", (now(), op, encode({"input": fields, "result": result})))
        return result

    def _library(self, con, member):
        return [{**m, "_order": i} for i, m in enumerate(self.parliament._all(con, "material")) if m["owner"] == member]

    def _user_review(self, con, state):
        if state.get("version", 3) < 4 or self._pending(state, "USER"):
            return
        failed = [j for j in state["jobs"].values() if j["state"] == "failed" and j["spec"]["role"] == "user_review"]
        for job in failed:
            if job.get("failure_kind") == "contract" and any(d.get("kind") == "contract_repair_exhausted" and d.get("job_id") == job["spec"]["job_id"] for d in state["diagnostics"]):
                job["state"] = "quarantined"
                state["user_collaboration"].setdefault("quarantined_batches", []).append({
                    "job_id":job["spec"]["job_id"],"event_ids":job["spec"]["metadata"]["user_context"]["pending_event_ids"],
                    "error":job.get("error"),"state":"quarantined"})
            else:
                return
        from . import user_collaboration as user
        pending_events = user.pending_events(state)
        meaningful = any(e["kind"] in {"user_reply","observation","review_requested"}
                         or e["kind"] == "downstream_use" and e.get("material_refs") for e in pending_events)
        if not meaningful:
            # Merely proposing/showing our own question is not a new observation
            # of the user. Retain it to join the later actual reply/use chain.
            state["topics"]["USER"]["state"] = "sleeping"
            return
        settings = context_policy(state)
        context = user.context_for_review(state, max_chars=settings["model_context_tokens"] * 8,
            max_events=max(1, len(state["user_collaboration"]["events"])),
            capacity_check=lambda prompt, schema: capacity(prompt, schema, settings=settings)["fits"])
        if not context["pending_event_ids"]:
            raise ProjectionBudgetError("USER linked observation bundle exceeds capacity; explicit source preparation required")
        self._job(state, "user_review", "USER", user.review_prompt(context), {"user_context": context}, schema=user.REVIEW_SCHEMA)
        state["topics"]["USER"]["state"] = "working"

    def _apply_user_review(self, con, state, spec, output, result):
        from . import user_collaboration as user
        user.apply_review(state, output, job_id=spec["job_id"], context=spec["metadata"]["user_context"],
            source={"kind": _result_source_kind(result),
                    "reference": result["result_id"]}, isolate_invalid=True)
        processed = set(spec["metadata"]["user_context"]["pending_event_ids"])
        for item in state["user_inputs"]:
            if item.get("topic_id") == "USER" and item.get("user_event_id") in processed:
                state["input_receipts"][item["id"]] = {"state":"processed", "job_id":spec["job_id"],
                    "effect":output.get("summary", "用户协作议题已处理此输入。"),"use":"user_review","history":[]}
        state["topics"]["USER"]["state"] = "sleeping"
        # Private judgments never enter ROOT's general summary. Only the
        # explicitly permitted material projection can reach ordinary roles.
        state["topics"]["USER"]["summary"] = "用户协作信息已处理；观点按用途和受众提供，不等于效果已证实。"
        # Explicit consult tool requests already carry a waiting dependency.
        # Other scoped guidance is offered on the next meaningful task turn;
        # profiling alone must not create a ROOT -> USER -> ROOT feedback loop.

    def _collaboration_materials(self, con, state, member, topic, query):
        if state.get("version", 3) < 4:
            return set()
        from . import user_collaboration as user
        selected = user.select_materials(state, purpose="collaboration", audience=member, topic_id=topic, query=query,
                                         max_chars=3500, max_items=5)
        allowed = set()
        for window, items in (("abstract", selected["A"]), ("scenario", selected["B"])):
            for item in items:
                mid = member + ".USER." + digest(item)[:20]
                allowed.add(mid)
                if not con.execute("SELECT 1 FROM objects WHERE id=?", (mid,)).fetchone():
                    self._op(con, "material", id=mid, owner=member, window=window, content=encode(item),
                             source={"kind": "model", "reference": "USER scoped asset:" + item["id"]},
                             tags=["user_collaboration", "conditional"])
        state.setdefault("user_material_deliveries", []).append({"member": member, "topic": topic, **selected["manifest"]})
        return allowed

    def _selection_library(self, con, state, member, *, topic, query):
        allowed_user = self._collaboration_materials(con, state, member, topic, query)
        originals = self._library(con, member)
        if state.get("version", 3) < 4:
            return originals
        from .memory import material_views
        from .context import _eligibility
        result = []
        views = material_views(state.get("memory"), originals)
        by_id = {m["id"]: m for m in views}
        # Recheck actual INPUT/TOOL ancestry even for wrappers persisted by an
        # earlier runtime. A catalog/view cannot revive a restricted original.
        checked_sources = self._sources(con, state, member=member, topic=topic,
            references=[m["id"] for m in originals if "runtime_original" in m.get("tags", [])])
        denied_origins = {item["id"] for item in checked_sources if item.get("denial_reason")}
        for item in views:
            if item["id"] in denied_origins or any(ref in denied_origins for ref in item.get("refs", [])):
                continue
            source_wrapper = item["id"].startswith("SOURCE.") and "runtime_original" in item.get("tags", [])
            if _eligibility(item, member, "design_association", {"topic": topic}):
                continue
            if "user_collaboration" in item.get("tags", []):
                denial, _ = self._source_permission(con, state, member, item["id"],
                    topic=topic, query=query, views=by_id)
                if denial or (item["id"] not in allowed_user and not source_wrapper):
                    continue
            applicability = item.get("applicability", {})
            if applicability.get("status") in {"retired", "superseded", "inapplicable"}:
                continue
            expected = applicability.get("context", {})
            if expected.get("topic") and expected["topic"] != topic:
                continue
            if applicability or not con.execute("SELECT 1 FROM objects WHERE id=?", (item["id"],)).fetchone():
                mid = "VIEW." + digest([member, item])[:24]
                if not con.execute("SELECT 1 FROM objects WHERE id=?", (mid,)).fetchone():
                    self._op(con, "material", id=mid, owner=member, content=item["content"] + "\n适用判断（非事实证明）：" + encode(applicability),
                        window=item["window"], form="relation", source={**item["source"], "reference": item["source"]["reference"] + "; canonical material:" + item["id"]},
                        refs=[item["id"]] if con.execute("SELECT 1 FROM objects WHERE id=?", (item["id"],)).fetchone() else [],
                        tags=list(dict.fromkeys(item.get("tags", []) + ["memory_generated", "current_view"])))
                if mid not in by_id or not _eligibility(by_id[mid], member, "design_association", {"topic": topic}):
                    result.append(self.parliament._get(con, mid, "material"))
            elif "current_view" not in item.get("tags", []) or source_wrapper:
                result.append(self.parliament._get(con, item["id"], "material"))
        unique = {}
        for item in result:
            prior = unique.get(item["id"])
            if prior is not None and prior != item:
                raise WorkflowError("conflicting originals for material id: " + item["id"])
            unique[item["id"]] = item
        return list(unique.values())

    def _memory_review(self, con, state):
        if state.get("version", 3) < 4:
            return
        from . import memory
        for member in state["members"]:
            # Finish the owner's current assessment wave before maintaining its
            # newly learned materials. Waiting is event-based, not a timer or
            # a minimum-volume rule; the final arrival triggers one review.
            if any(j['state'] == 'pending' and j['spec']['role'] == 'assessment'
                   and j['spec']['metadata'].get('member_id') == member['id'] for j in state['jobs'].values()):
                continue
            if any(j["spec"]["role"] == "memory_review" and j["spec"]["metadata"].get("member_id") == member["id"]
                   and j["state"] in {"pending", "failed"} for j in state["jobs"].values()):
                continue
            materials = self._library(con, member["id"])
            if not memory.pending_material_ids(state.get("memory"), materials):
                continue
            task = {"goal": state["goal"], "instruction": "检查新经验与已有经验的关系，允许无需变更；不得以维护代替完成当前目标。"}
            settings = context_policy(state)
            bound = settings["model_context_tokens"] * 8
            def maintenance_fits(candidate):
                payload = attach({"rules": memory.MEMORY_RULES, "task":candidate, "output_contract":memory.MEMORY_SCHEMA}, "memory_review")
                return capacity(encode(payload), memory.MEMORY_SCHEMA, settings=settings)["fits"]
            try:
                context = memory.maintenance_context(state.get("memory"), materials, task, member=member["id"], max_chars=bound, capacity_check=maintenance_fits)
            except ProjectionBudgetError:
                # Oversized learned experience gets the same isolated, reviewed
                # memory treatment. Original material and version stay immutable.
                prepared = deepcopy(materials)
                pending = set(memory.pending_material_ids(state.get("memory"), materials))
                for m in prepared:
                    if m["id"] in pending:
                        body, units, assurance = self._compact_text(state, "ROOT", "memory_review:"+member["id"], encode(m),
                            context_policy(state)["summary_target_chars"],
                            fits=lambda body: capacity(body, memory.MEMORY_SCHEMA,
                                settings={**settings, "platform_reserve_tokens":settings["platform_reserve_tokens"] + 16000})["fits"])
                        if units:
                            m["content"] = "经独立复核的经验摘要，非原文：\n" + body
                            m["semantic_memory"] = {"original_id":m["id"],"unit_ids":units,"assurance":assurance}
                context = memory.maintenance_context(state.get("memory"), prepared, task, member=member["id"], max_chars=bound, capacity_check=maintenance_fits)
            if not context["new_material_ids"]:
                continue
            prompt, schema, packet = self._memory_packet(context, settings=settings)
            self._job(state, "memory_review", "ROOT", prompt, {"member_id": member["id"], "memory_context": context,
                      "input_packet": packet}, schema=schema)

    def _memory_packet(self, context, *, validation_failure=None, settings=None):
        from . import memory
        schema = deepcopy(memory.MEMORY_SCHEMA)
        payload = attach({"rules": memory.MEMORY_RULES, "task": deepcopy(context), "output_contract": schema}, "memory_review")
        if validation_failure is not None:
            payload["validation_failure"] = deepcopy(validation_failure)
        prompt = encode(payload)
        if not capacity(prompt, schema, settings=settings)["fits"]:
            raise ProjectionBudgetError("maintenance input exceeds physical capacity; source paging required", chars=len(prompt))
        protocol = digest([payload["rules"], payload["cognitive_protocol"], schema])
        return prompt, schema, {"sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "protocol_version": "sha256:" + protocol, "protocol_sha256": protocol,
            "task_sha256": digest(context), "explicit_chars": len(prompt)}

    def _apply_memory_review(self, con, state, spec, output, result):
        from . import memory
        context = spec["metadata"]["memory_context"]
        if set(output["reviewed_material_ids"]) != set(context["new_material_ids"]):
            raise WorkflowError("memory review must explicitly process its sealed new material set")
        state["memory"] = memory.apply_maintenance(state.get("memory"), output, self._library(con, spec["metadata"]["member_id"]),
            source_ids=context["available_source_ids"], source={"kind": _result_source_kind(result), "reference": result["result_id"]},
            visible_material_ids=[m["id"] for m in context["materials"]], new_material_ids=context["new_material_ids"])
        # Maintenance changes future retrieval, not the business decision.
        # Do not schedule a parent just to acknowledge administrative completion.

    def question_presented(self, question_id, *, channel="operator", source="operator"):
        from . import user_collaboration as user
        with self._tx() as (con, state):
            question = state["questions"].get(question_id)
            if not question or question["state"] in {"proposed","linked"} or not (
                    question["state"] in {"open","answered"} or question.get("publication") or question.get("delivery")):
                raise WorkflowError("only a published question can be presented")
            if state.get("version", 3) >= 4:
                user.record_event(state, question_id + ".PRESENTED", "question_presented", source=source,
                    topic_id=question["topic_id"], question_id=question_id, content=question["text"],
                    allowed_uses=["profile", "collaboration"],
                    audience=user.event_audience(state, question.get("audience", ["*"]),
                        links=[question_id + ".PROPOSED"], material_refs=question.get("guidance_ids", [])),
                    links=[question_id + ".PROPOSED"],
                    material_refs=question.get("guidance_ids", []))
                question.setdefault("delivery", {"channel": channel, "at": now(), "claim": "presented, not necessarily understood"})
                self._user_review(con, state)
            return dict(question)

    def _record_input_uses(self, con, state, spec, output, result=None):
        if state.get("version", 3) < 4:
            return
        from . import user_collaboration as user
        visible = {x["id"]: x for x in json.loads(spec["prompt"])["task"].get("user_inputs", []) if "id" in x}
        prior = deepcopy(state.get("input_receipts", {}))
        self._acknowledge_inputs(state, spec, output)
        for use in output.get("input_uses", []):
            ref = use["input_id"]
            original = visible[ref]
            eid = original.get("user_event_id")
            previous = prior.get(ref, {})
            repeated = (previous.get("state") == "processed" and use.get("disposition") == "processed"
                and (output.get("progress", {}).get("kind") == "no_change" or
                     all(previous.get(k) == use.get(k) for k in ("use", "effect"))))
            if eid and not repeated:
                evidence = next(e for e in state["user_collaboration"]["events"] if e["id"] == eid)
                user.record_event(state, spec["job_id"] + ".USE." + ref, "downstream_use", source="model",
                    topic_id=spec["metadata"]["topic_id"], job_id=spec["job_id"], revision_id=spec["metadata"]["revision"],
                    content=encode(use), links=[eid], allowed_uses=evidence["allowed_uses"], audience=evidence["audience"],
                    material_refs=evidence.get("material_refs", []))
                if use.get("disposition") == "processed" and output.get("files") and result and _result_source_kind(result) != "fixture":
                    # Committed in the same semantic transaction as these exact
                    # artifact bytes. It proves a downstream output, not benefit.
                    user.record_event(state, spec["job_id"] + ".COMMITTED." + ref, "downstream_use", source="runtime",
                        topic_id=spec["metadata"]["topic_id"], job_id=spec["job_id"], revision_id=spec["metadata"]["revision"],
                        content=encode({"claim":"actual artifact commit linked to model-declared uptake; no causal effectiveness claim",
                            "artifacts":[{"path":"artifacts/"+spec["job_id"]+"/"+f["path"],"sha256":hashlib.sha256(f['content'].encode('utf-8')).hexdigest()} for f in output["files"]]}),
                        links=[eid], allowed_uses=evidence["allowed_uses"], audience=evidence["audience"], material_refs=evidence.get("material_refs", []))

    def _return_source(self, con, state, returned):
        if returned in state["return_sources"]:
            return state["return_sources"][returned]
        record = self.parliament._get(con, returned, "return")
        for jid, job in state["jobs"].items():
            if job["spec"]["metadata"].get("revision") != record["revision"] or not job.get("result_id"):
                continue
            row = con.execute("SELECT body FROM results WHERE result_id=?", (job["result_id"],)).fetchone()
            payload = json.loads(row[0]).get("payload", {}) if row else {}
            if payload.get("ans") == record["ans"] and payload.get("contributions") == record["contributions"]:
                state["return_sources"][returned] = jid
                return jid
        raise WorkflowError("return has no traceable author result: " + returned)

    def _sources(self, con, state, *, references=(), text="", member=None, topic=None, obligation_catalog_ref=None):
        """Resolve only exact known IDs/names from the current run, never files."""
        artifacts = {r[0]: {"id": r[0], "source_job": r[1], "kind": "artifact"}
                     for r in con.execute("SELECT path,source_job FROM artifacts")}
        objects = {o["id"]: o for kind in ("return", "assessment", "revision", "material", "candidate", "disposition")
                   for o in self.parliament._all(con, kind)}
        inputs = {x["id"]: x for x in state["user_inputs"] if x.get("kind") == "tool_result"}
        tool_requests = {item["id"]: item for item in state["tool_requests"]}
        allowed_topics = self._scope(state, topic) if topic is not None else set(state["topics"])
        allowed_topics.discard("USER")

        def protected_record(value):
            if not isinstance(value, dict):
                return False
            tags, purpose = value.get("tags"), value.get("purpose")
            return (value.get("adapter") == "user_collaboration" or value.get("owner") == "USER"
                or isinstance(tags, list) and any(isinstance(tag, str) and tag in {"profile_only", "user_collaboration"} for tag in tags)
                or isinstance(purpose, str) and purpose in {"profile_only", "user_profile_only"})

        def protected(value):
            if isinstance(value, list):
                return any(protected(item) for item in value)
            if not isinstance(value, dict):
                return False
            if protected_record(value):
                return True
            return any(protected(item) for item in value.values() if isinstance(item, (dict, list)))

        def tool_denial(item, seen=()):
            if item.get("id") in seen:
                return "cyclic tool source provenance"
            if item.get("topic_id") not in allowed_topics:
                return "tool original is outside the requesting topic scope"
            request = tool_requests.get(item.get("request_id"), item)
            source_job = state["jobs"].get(request.get("source_job"), {}).get("spec", {})
            if (request.get("kind") == "user.consult" or source_job.get("role") == "user_review"
                    or protected(item)):
                return "USER consultation original is restricted; use currently authorized scoped USER materials"
            if request.get("kind") == "cognition.process":
                plan = state.get("cognitive_operations", {}).get(request.get("id"))
                if plan is None:
                    return "cognitive output has no verifiable source plan"
                for index, selected in enumerate(plan["sources"] + plan["constraints"] + plan["withheld_sources"]):
                    for condition in selected.get("inherited_conditions", []):
                        audience = condition.get("audience")
                        if audience and member is not None and not ({"*", member} & set(audience)):
                            return "cognitive source audience excludes the receiving member"
                    # Check original ancestors, including USER markers and tool
                    # provenance, rather than trusting a paraphrased derivative.
                    original = selected["source"]
                    probe = {"id": request["id"] + ".SOURCE." + str(index), "kind":"context.retrieve",
                        "topic_id": plan["topic_id"], "source_job": plan["source_job"],
                        "arguments":{"reference":{"source_sha256":original["source_sha256"],"pointer":original.get("permission_pointer",original["pointer"])}}}
                    denial = tool_denial(probe, (*seen, item.get("id")))
                    if denial:
                        return "cognitive source restriction: " + denial
            if request.get("kind") in {"context.retrieve", "context.catalog"}:
                args = request.get("arguments", {})
                if request["kind"] == "context.retrieve" and isinstance(args, dict) and "path" in args:
                    row = con.execute("SELECT source_job FROM artifacts WHERE path=?", (args["path"],)).fetchone()
                    if row is None or row[0] != request.get("source_job"):
                        return "generated original is outside the producing call's scope"
                    return None
                reference = args.get("reference", {}) if isinstance(args, dict) else None
                if not isinstance(reference, dict):
                    return "sealed context reference is invalid for scope validation"
                sha = reference.get("source_sha256") or source_job.get("metadata", {}).get("context_projection", {}).get("source_sha256")
                original = state["context_snapshots"].get(sha) if isinstance(sha, str) else None
                if original is None:
                    return "sealed context original is unavailable for scope validation"
                pointer = reference.get("pointer", "") if request["kind"] == "context.retrieve" else ""
                if not isinstance(pointer, str) or pointer and not pointer.startswith("/"):
                    return "sealed context pointer is invalid for scope validation"
                value = original
                try:
                    # Inspect provenance ancestors, even for a character range
                    # whose returned text is not a parseable JSON document.
                    for token in pointer.split("/")[1:] if pointer else []:
                        if protected_record(value):
                            return "sealed context points inside restricted USER material; use currently authorized scoped USER materials"
                        if isinstance(value, dict) and value.get("kind") == "tool_result":
                            denial = tool_denial(value, (*seen, item.get("id")))
                            if denial:
                                return denial
                        token = token.replace("~1", "/").replace("~0", "~")
                        value = value[int(token)] if isinstance(value, list) else value[token]
                except (KeyError, IndexError, TypeError, ValueError):
                    return "sealed context pointer is unavailable for scope validation"
                if protected(value):
                    return "sealed context contains restricted USER originals; use currently authorized scoped USER materials"
                if isinstance(value, dict) and value.get("kind") == "tool_result":
                    return tool_denial(value, (*seen, item.get("id")))
            return None

        def wrapper_tool_denial(item, seen=()):
            if item["id"] in seen:
                return "cyclic original source wrapper"
            if item["id"].startswith("SOURCE.") and "runtime_original" in item.get("tags", []):
                try:
                    provenance = json.loads(item["source"]["reference"])
                    if not isinstance(provenance, dict):
                        raise ValueError("source provenance must be an object")
                except (KeyError, TypeError, ValueError):
                    return ("unverifiable tool source wrapper" if {"tool_result", "tool_request"} & set(item.get("tags", [])) else None)
                original_id = provenance.get("object_id") or provenance.get("id")
                if provenance.get("kind") == "obligation_catalog":
                    import re
                    locator = re.fullmatch(r"OBLIGATION\.CATALOG\.([0-9a-f]{64})\.PAGE\.(0|[1-9][0-9]{0,9})", str(original_id))
                    if not locator:
                        return "unverifiable obligation catalog source wrapper"
                    original = self._sources(con, state, member=member, topic=topic,
                        text="obligation_catalog cursor=" + locator[2] + " snapshot=" + locator[1])[0]
                    if original.get("denial_reason"):
                        return original["denial_reason"]
                    body = original["content"]
                    if (provenance.get("sha256") != hashlib.sha256(body.encode()).hexdigest()
                            or provenance.get("total_chars") != len(body)
                            or not isinstance(provenance.get("start"), int) or not isinstance(provenance.get("end"), int)
                            or not 0 <= provenance["start"] <= provenance["end"] <= len(body)
                            or item["content"] != body[provenance["start"]:provenance["end"]]):
                        return "obligation catalog source wrapper does not match its exact sealed page"
                if original_id in inputs or original_id in tool_requests:
                    return tool_denial(inputs.get(original_id) or tool_requests[original_id])
                if original_id in objects and objects[original_id]["kind"] == "material":
                    return wrapper_tool_denial(objects[original_id], (*seen, item["id"]))
            if {"current_view", "memory_generated"} & set(item.get("tags", [])):
                for ref in item.get("refs", []):
                    if ref in objects and objects[ref]["kind"] == "material":
                        denial = wrapper_tool_denial(objects[ref], (*seen, item["id"]))
                        if denial:
                            return denial
            return None

        import re
        catalog_results = []
        catalog_queries = re.findall(r"obligation_catalog\s+cursor=([^\s,\"';]+)(?:\s+snapshot=([^\s,\"';]+))?", text)
        for cursor_text, requested_sha in dict.fromkeys(catalog_queries):
            sha = requested_sha or obligation_catalog_ref
            ref = "OBLIGATION.CATALOG." + str(sha) + ".PAGE." + cursor_text
            snapshot = state.get("obligation_catalogs", {}).get(sha) if isinstance(sha, str) else None
            reason = None
            if not snapshot or not re.fullmatch(r"[0-9a-f]{64}", sha) or digest(snapshot) != sha:
                reason = "unknown or unverifiable obligation catalog snapshot; use the sealed inquiry locator"
            elif member not in snapshot["members"] or topic not in state["topics"] or topic == "USER":
                reason = "obligation catalog is outside the requesting member/topic scope"
            else:
                scope = snapshot["scope"]
                current = state["topics"][topic]
                child = state["topics"].get(scope["child"]) if scope["child"] else None
                if (scope["topic_id"] != topic or scope["revision"] != current["revision"]
                        or scope["epoch"] != state["epoch"] or scope["topic_epoch"] != current.get("epoch", 0)
                        or scope["child"] and (not child or child.get("parent") != topic
                            or child["revision"] != scope["child_revision"] or child.get("epoch", 0) != scope["child_epoch"])):
                    reason = "obligation catalog is outside the current topic/revision/epoch scope"
            if reason is None:
                visible = [entry for entry in snapshot["entries"] if member in entry["visible_to"]]
                if (not re.fullmatch(r"0|[1-9][0-9]{0,9}", cursor_text)
                        or int(cursor_text) >= len(visible) and (visible or cursor_text != "0")):
                    reason = "obligation catalog cursor is outside the sealed visible directory"
                else:
                    cursor = int(cursor_text)
                    entries = visible[cursor:cursor + 8]
                    current_items = {item["id"]: item for item in state["obligations"]}
                    for entry in entries:
                        item = current_items.get(entry["id"], {})
                        job = state["jobs"].get(item.get("source_job"), {}).get("spec", {})
                        owner = item.get("member_id") or (job.get("metadata", {}).get("member_id") if job.get("role") == "member" else None)
                        audience = item.get("audience", ["*"])
                        if (item.get("topic_id") != topic or owner not in {None, member}
                                or job.get("role") == "user_review" or job.get("metadata", {}).get("topic_id") == "USER"
                                or item.get("owner") == "USER" or not isinstance(audience, list)
                                or not ({"*", member} & set(audience))):
                            reason = "obligation catalog entry is no longer visible to this member/topic"
                            break
            if reason:
                catalog_results.append({"id": ref, "kind": "obligation_catalog", "denial_reason": reason,
                    "alternative": "Use the current inquiry's own obligation_catalog locator and an in-range cursor."})
                continue
            end = cursor + len(entries)
            page = {"source": {"kind": "sealed_obligation_directory", "sha256": sha, "scope": snapshot["scope"], "member_id": member},
                    "cursor": cursor, "next_cursor": end if end < len(visible) else None, "total": len(visible),
                    "entries": [{"id": entry["id"], "state": entry["state"]} for entry in entries],
                    "boundary": "Historical ID/state directory only; no obligation body, current reading or fulfillment is implied."}
            catalog_results.append({"id": ref, "kind": "obligation_catalog", "content": encode(page)})

        chosen = list(references)
        # Exact names are only a deterministic resolver, not a relevance claim.
        def mentioned_path(value):
            # A basename inside a qualified path must not select every version.
            # Bare names still request all matching versions; punctuation and
            # Chinese prose may surround either literal, but path continuations
            # (including .bak) are not a match for a shorter stored filename.
            return re.search(r"(?<![A-Za-z0-9_./\\-])" + re.escape(value)
                             + r"(?![A-Za-z0-9_/\\-]|\.[A-Za-z0-9_])", text)

        for path in artifacts:
            if mentioned_path(path) or mentioned_path(PurePosixPath(path).name):
                chosen.append(path)
        for match in re.findall(r"[A-Za-z][A-Za-z0-9_.:-]*", text):
            if match.rstrip(".") in objects or match.rstrip(".") in inputs or match.rstrip(".") in tool_requests:
                chosen.append(match.rstrip("."))
        for base in re.findall(r"([A-Za-z][A-Za-z0-9_.:-]*\.)A/B", text):
            chosen.extend([base + "A", base + "B"])
        results, seen = catalog_results, set()
        while chosen:
            ref = chosen.pop(0)
            if ref in seen:
                continue
            seen.add(ref)
            if ref in artifacts:
                item = dict(artifacts[ref])
                item['content'] = con.execute('SELECT content FROM artifacts WHERE path=?', (ref,)).fetchone()[0]
                item["result_id"] = state["jobs"][item["source_job"]].get("result_id")
                results.append(item)
            elif ref in inputs or ref in tool_requests:
                item = inputs.get(ref) or tool_requests[ref]
                denial = tool_denial(item)
                if denial:
                    results.append({"id": ref, "kind": "tool_result" if ref in inputs else "tool_request",
                        "denial_reason": denial,
                        "alternative": ("Request currently authorized scoped USER materials; raw consultation/context wrappers do not widen the audience."
                            if "USER" in denial else "Request an original within the current topic scope; unavailable sealed provenance cannot grant access.")})
                    continue
                results.append({"id": ref, "kind": "tool_result" if ref in inputs else "tool_request", "content": encode(item)})
            elif ref in objects:
                obj = objects[ref]
                if obj["kind"] == "material" and member and obj["owner"] != member:
                    # A peer's private library is not opened by a mention.
                    if state.get("version", 3) >= 4:
                        results.append({"id": ref, "kind": "material", "object_id": ref,
                                        "denial_reason": "outside member ownership"})
                    continue
                if obj["kind"] == "material" and member:
                    denial = wrapper_tool_denial(obj)
                    if denial:
                        results.append({"id": ref, "kind": "material", "object_id": ref, "denial_reason": denial,
                            "alternative": "Use currently authorized originals/scoped USER materials; old wrapper IDs do not preserve revoked or wider permissions."})
                        continue
                    from .context import _eligibility
                    denial = _eligibility(obj, member, "design_association", {})
                    if denial:
                        if state.get("version", 3) >= 4:
                            results.append({"id": ref, "kind": "material", "object_id": ref, "denial_reason": denial})
                        continue
                results.append({"id": ref, "kind": obj["kind"], "content": encode(obj), "object_id": ref,
                                "inherited_tags": obj.get("tags", [])})
                if obj["kind"] == "return":
                    try:
                        jid = self._return_source(con, state, ref)
                    except WorkflowError:
                        continue
                    chosen.extend(p for p, a in artifacts.items() if a["source_job"] == jid)
                elif obj["kind"] == "assessment":
                    chosen.append(obj["returned"])
                    source_job = state["assessment_sources"].get(obj["id"])
                    if source_job is None:
                        for learning in self.parliament._all(con, "learning"):
                            if learning["assessment"] == obj["id"]:
                                source_record = self.parliament._get(con, learning["materials"][0], "material")["source"]["reference"]
                                match = re.search(r"job:(JOB\.\d+)", source_record)
                                source_job = match.group(1) if match else None
                    for job in state["jobs"].values():
                        meta = job["spec"]["metadata"]
                        if job["spec"]["job_id"] == source_job:
                            p = json.loads(job["spec"]["prompt"])
                            # This is an exact visibility manifest, not a claim
                            # that the manifest transmits each referenced body.
                            scope = {"job_id": job["spec"]["job_id"], "prompt_sha256": hashlib.sha256(job["spec"]["prompt"].encode()).hexdigest(),
                                     "task": p["task"], "visible_materials": [{"id": m["id"], "source": m["source"],
                                     "content_chars": len(m["content"])} for m in p["A"] + p["B"]],
                                     "boundary": "Exact input scope only; cited IDs do not imply their originals are reproduced here."}
                            results.append({"id": job["spec"]["job_id"] + ".INPUT_SCOPE", "kind": "input_scope", "content": encode(scope)})
        return results

    def _source_permission(self, con, state, member, material_id, *, topic, query, views=None, seen=()):
        """Check immutable USER delivery ancestry, not just a wrapper's ID/tag.

        An explicit original request may use a currently permitted asset even
        when the optional USER selection budget did not choose that asset.
        It still consumes the normal A/B evidence budget after wrapping.
        """
        from .context import _eligibility
        from .memory import material_views
        from . import user_collaboration as user
        if views is None:
            views = {m["id"]: m for m in material_views(state.get("memory"), self.parliament._all(con, "material"))}
        item = views.get(material_id)
        if item is None or material_id in seen:
            return "missing or cyclic material source chain", set()
        denial = _eligibility(item, member, "design_association", {"topic": topic} if topic else {})
        if denial:
            return denial, set()
        reference = item.get("source", {}).get("reference", "")
        prefix = "USER scoped asset:"
        source_wrapper = material_id.startswith("SOURCE.") and "runtime_original" in item.get("tags", [])
        if "user_collaboration" not in item.get("tags", []) and not reference.startswith(prefix):
            return None, set()
        if reference.startswith(prefix) and "current_view" not in item.get("tags", []):
            asset_id = reference[len(prefix):]
            data = state.get("user_collaboration", {})
            asset = data.get("assets", {}).get(asset_id)
            if not asset:
                return "USER source asset is unavailable", set()
            if asset_id in data.get("superseded_by", {}) or asset["status"] == "retracted":
                return "USER source is a historical version", set()
            if asset.get("source", {}).get("kind") == "fixture" or "fixture" in asset["provenance"]["sources"]:
                return "USER source is fixture_only", set()
            if not ({"*", "collaboration"} & set(asset["allowed_uses"])):
                return "USER source is profile_only or outside collaboration purpose", set()
            if not ({"*", member} & set(asset["audience"])):
                return "USER source is outside member audience", set()
            terms = query.lower().split()
            if topic and not (topic in asset["topic_ids"] or "*" in asset["topic_ids"] or
                    terms and any(t in (asset["content"] + " " + asset["conditions"]).lower() for t in terms)):
                return "USER source is outside current topic/query scope", set()
            material = user._material(asset)
            if material_id != member + ".USER." + digest(material)[:20] or item["content"] != encode(material):
                return "USER source does not match its original scoped delivery", set()
            return None, {asset_id}
        refs = [ref for ref in item.get("refs", []) if ref in views]
        if source_wrapper:
            try:
                provenance = json.loads(reference)
                original_id = provenance["object_id"]
                original = self.parliament._get(con, original_id, "material")
                body = encode(original)
                valid = (item.get("refs") == [original_id] and original_id in views and
                    material_id == "SOURCE." + digest([member, provenance])[:24] and
                    provenance["sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest() and
                    provenance["total_chars"] == len(body) and
                    0 <= provenance["start"] <= provenance["end"] <= len(body) and
                    item["content"] == (body[provenance["start"]:provenance["end"]] or "[empty original]"))
            except (ValueError, KeyError, TypeError):
                valid = False
            if not valid:
                return "USER source wrapper does not match its immutable original", set()
        if "current_view" in item.get("tags", []) and not source_wrapper:
            canonical = reference.rsplit("; canonical material:", 1)[-1]
            if canonical not in views or material_id != "VIEW." + digest([member, views[canonical]])[:24]:
                return "USER view is no longer the current source interpretation", set()
            refs.append(canonical)
        user_ids = set()
        for ref in dict.fromkeys(refs):
            denial, inherited = self._source_permission(con, state, member, ref,
                topic=topic, query=query, views=views, seen=(*seen, material_id))
            if denial:
                return denial, set()
            user_ids.update(inherited)
        if "user_collaboration" in item.get("tags", []):
            if not user_ids:
                return "untraceable USER material source chain", set()
            if item.get("source", {}).get("kind") == "fixture":
                return "USER-derived interpretation is fixture_only", set()
        return None, user_ids

    def _source_materials(self, con, state, member, sources, *, topic=None, query=""):
        ids, manifest = [], []
        for source in sources:
            if source.get("denial_reason"):
                manifest.append({"id": source["id"], "object_id": source.get("object_id"),
                    "state": "denied", "body_delivered": False, "reason": source["denial_reason"],
                    "alternative": source.get("alternative", "Use an original authorized for this member and topic; denial does not mean absent.")})
                continue
            if state.get("version", 3) >= 4 and source.get("object_id"):
                row = con.execute("SELECT kind FROM objects WHERE id=?", (source["object_id"],)).fetchone()
                if row and row[0] == "material":
                    denial, _ = self._source_permission(con, state, member, source["object_id"], topic=topic, query=query)
                    if denial:
                        manifest.append({"id": source["id"], "object_id": source["object_id"],
                            "state": "denied", "body_delivered": False, "reason": denial})
                        continue
            body = source["content"]
            source_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
            # Explicit lossless ranges, not silent summarization/truncation.
            chunk_size = max(400, min(16000, self.parliament._get(con, "CONFIG", "config")["limits"]["scenario_chars"] // 4))
            for start in range(0, max(1, len(body)), chunk_size):
                end = min(start + chunk_size, len(body))
                provenance = {k: v for k, v in source.items() if k != "content"}
                provenance.update(sha256=source_hash, start=start, end=end, total_chars=len(body), complete=start == 0 and end == len(body))
                mid = "SOURCE." + digest([member, provenance])[:24]
                if not con.execute("SELECT 1 FROM objects WHERE id=?", (mid,)).fetchone():
                    self._op(con, "material", id=mid, owner=member, content=body[start:end] or "[empty original]", window="scenario",
                             source={"kind": "observation", "reference": encode(provenance)},
                             refs=[source["object_id"]] if source.get("object_id") else [], tags=list(dict.fromkeys(["runtime_original", source["kind"]] + source.get("inherited_tags", []))))
                ids.append(mid)
                manifest.append({"material_id": mid, **provenance})
        return list(dict.fromkeys(ids)), manifest

    def _artifact_views(self, con, state, topic, role):
        scope = self._scope(state, topic)
        own_jobs = [j['spec']['job_id'] for j in state['jobs'].values() if j['state'] == 'applied'
                    and j['spec']['role'] == role and j['spec']['metadata']['topic_id'] == topic]
        focus = set(own_jobs[-1:]) | {state['return_sources'].get(r) for r in state['topics'][topic]['new_returns']}
        focus.update(i.get('source_job') for i in state['user_inputs'] if i.get('topic_id') in {None,topic}
                     and state.get('input_receipts', {}).get(i['id'], {}).get('state') != 'processed')
        headers = {r[0]:dict(path=r[0],source_job=r[1],sha256=r[2],content_chars=r[3],content_bytes=r[4])
                   for r in con.execute('SELECT path,source_job,sha256,chars,bytes FROM artifact_headers')}
        result = []
        for path, jid in con.execute('SELECT path,source_job FROM artifacts'):
            if state['jobs'].get(jid, {}).get('spec', {}).get('metadata', {}).get('topic_id') not in scope: continue
            header, content = headers.get(path), None
            if header is None or jid in focus or not own_jobs:
                content = con.execute('SELECT content FROM artifacts WHERE path=?', (path,)).fetchone()[0]
            if header is None:
                header = dict(path=path,source_job=jid,sha256=hashlib.sha256(content.encode('utf-8')).hexdigest(),
                              content_chars=len(content),content_bytes=len(content.encode('utf-8')))
                con.execute('INSERT INTO artifact_headers VALUES(?,?,?,?,?)',
                            (path,jid,header['sha256'],header['content_chars'],header['content_bytes']))
            entry = {**header,'hash_representation':'UTF-8 file bytes',
                     'body_delivered':jid in focus or not own_jobs,'retrieve':'artifact.read or bind this exact locator to cognition.process'}
            if entry['body_delivered']: entry['content'] = content
            result.append(entry)
        return result

    def _public_evidence(self, con, state, topic, role='author'):
        # Match source retrieval and file ownership. A child assuming a parent
        # role must not inherit its grandparent's entire sibling corpus.
        scope = self._scope(state, topic)
        returns = [r for r in self.parliament._all(con, "return") if r["topic"] in scope]
        return_ids = {r["id"] for r in returns}
        return {"returns": returns, "assessments": [a for a in self.parliament._all(con, "assessment") if a["returned"] in return_ids],
                "assessment_scopes": [{"assessment_id": aid, "job_id": jid,
                    "returned": state["jobs"][jid]["spec"]["metadata"].get("returned"),
                    "evidence_material_ids": state["jobs"][jid]["spec"]["metadata"].get("evidence_material_ids", []),
                    "batch": state["jobs"][jid]["spec"]["metadata"].get("evidence_batch"),
                    "batches": state["jobs"][jid]["spec"]["metadata"].get("evidence_batches")}
                    for aid, jid in state["assessment_sources"].items()
                    if state["jobs"][jid]["spec"]["metadata"].get("returned") in return_ids],
                "artifacts": [{**a,'id':a['path'],'result_id':state['jobs'][a['source_job']].get('result_id')}
                              for a in self._artifact_views(con,state,topic,role)],
                "delivery_manifests": [d for d in state["evidence_deliveries"] if any(
                    state["jobs"].get(b["job_id"], {}).get("spec", {}).get("metadata", {}).get("topic_id") in scope
                    for b in d["batches"])],
                "boundary": "Actual public originals in this topic's owned subtree; sibling bodies and private peer memories are not implicitly shared."}

    def _job(self, state, role, topic, prompt, metadata=None, *, schema=None):
        if role in {"parent", "author"}:
            task = json.loads(prompt)["task"]
            source = task.get("context_projection", {}).get("source_sha256")
            validate_input_task(task, original=state.get("context_snapshots", {}).get(source))
        cognitive_manifest = inspect_prompt(prompt, role)
        schema = schema or runtime_schema(role, v4=state.get("version", 3) >= 4)
        measured = capacity(prompt, schema, images=len(json.loads(prompt).get('task', {}).get('native_images', [])), settings=context_policy(state))
        if not measured["fits"]:
            raise ProjectionBudgetError("model physical input capacity exceeded; semantic preparation or source paging required", accounting=measured)
        jid = self._id(state, "JOB")
        meta = {"topic_id": topic, "revision": state["topics"][topic]["revision"], "epoch": state["epoch"],
                "fresh_context_required": True, "topic_epoch": state["topics"][topic].get("epoch", 0),
                "parent_decision_version": state["topics"][topic].get("decision_version", 0), **(metadata or {})}
        meta["cognitive_protocol"] = cognitive_manifest
        meta["capacity"] = measured
        meta["input_ids"] = [x["id"] for x in json.loads(prompt).get("task", {}).get("user_inputs", []) if x.get("id")]
        meta['sealed_tool_inputs'] = [{'input_id':x['id'],'input_sha256':digest(x)}
            for x in json.loads(prompt).get('task', {}).get('user_inputs', []) if x.get('kind') == 'tool_result']
        images = json.loads(prompt).get('task', {}).get('native_images', [])
        if images:
            meta['native_images'] = images
        spec = {"job_id": jid, "run_id": state["run_id"], "kind": role, "role": role,
                "prompt": prompt, "schema": schema or runtime_schema(role, v4=state.get("version", 3) >= 4), "metadata": meta}
        state["jobs"][jid] = {"spec": spec, "state": "pending", "created_at": now()}
        memory = json.loads(prompt).get("task", {}).get("semantic_memory")
        if memory:
            state["context_memory"].setdefault("uses", []).append({"job_id":jid,"role":role,"topic_id":topic,
                "unit_ids":memory["unit_ids"],"original_source":memory["original_source"],
                "claim":"sealed into this job; actual acceptance and input uptake are separate"})
        replacement = state["topics"][topic].get("capacity_replacement", {}).pop(role, None)
        if replacement:
            spec["metadata"]["retry_of"] = replacement
            self._replace_active_job(state, replacement, jid)
        repair = state["topics"][topic].get("pending_role_repair", {}).pop(role, None)
        if repair:
            spec["metadata"].update(retry_of=repair["previous_job"], previous_error=repair["error"])
            spec["metadata"]["repair_kind"] = repair.get("kind", "output_contract")
            self._replace_active_job(state, repair["previous_job"], jid)
        return jid

    def _schedule_intent(self, state, kind, topic, **arguments):
        """Commit responsibility before attempting a bounded follow-up packet."""
        current = state["topics"][topic]
        identity = {"kind": kind, "topic_id": topic, "revision": current["revision"],
                    "epoch": state["epoch"], "topic_epoch": current.get("epoch", 0), "arguments": arguments}
        for item in state["schedule_intents"]:
            if item["state"] == "pending" and all(item[key] == value for key, value in identity.items()):
                return item["id"]
        sid = self._id(state, "SCHEDULE")
        state["schedule_intents"].append({"id": sid, "state": "pending", **identity})
        return sid

    def _run_schedule_intent(self, con, state, intent_id):
        intent = next(i for i in state["schedule_intents"] if i["id"] == intent_id)
        topic = state["topics"][intent["topic_id"]]
        if (intent["epoch"] != state["epoch"] or intent["revision"] != topic["revision"] or
                intent["topic_epoch"] != topic.get("epoch", 0)):
            intent["state"] = "superseded"
            return
        args = intent["arguments"]
        if intent["kind"] == "author":
            self._author(con, state, topic["id"])
            topic["author_refresh_required"] = False
        elif intent["kind"] == "inquiry":
            self._inquiry(con, state, topic["id"], **args)
            for request in state["obligations"]:
                if request["id"] in args.get("responding_to", []):
                    request["state"] = "delivery_prepared"
                    request["delivery_receipts"] = [{'request_id':d['request_id'],'member_id':d['member_id'],
                        'job_ids':[b['job_id'] for b in d['batches']]} for d in state['evidence_deliveries']
                        if request['id'] in d.get('responding_to', [])]
                    request["delivery_claim"] = "Actual source ranges are sealed; consumption and adequacy are not inferred."
        elif intent["kind"] == "assessments":
            self._schedule_assessments(con, state, state["jobs"][args["source_job"]]["spec"], args["returned"],
                                       {"contributions": args["contributions"]})
        elif intent["kind"] == "cognitive_compare":
            self._cognitive_compare(state, args["request_id"])
        else:
            raise WorkflowError("unknown persisted scheduling intent")
        intent["state"] = "scheduled"

    def _drain_schedule_intents(self, con, state):
        complete = True
        # Reacquire each record after a rollback replaces state; never retain a
        # mutable topic/intent reference across independent scheduling calls.
        for intent_id in [i["id"] for i in state["schedule_intents"] if i["state"] == "pending"]:
            complete = self._budgeted_schedule(con, state, "intent:" + intent_id,
                lambda: self._run_schedule_intent(con, state, intent_id)) and complete
        return complete

    def _inquiry_prior_ranges(self, state, scope, member, responding_to):
        """Only explicit reselect ancestry can advance an exact source range.

        Successful historical transport is ordering evidence, never proof that a
        fresh role has read it. No other topic/member/revision inherits this set.
        """
        obligations = {item["id"]: item for item in state["obligations"]}
        pending, seen, ranges = list(responding_to), set(), set()
        while pending:
            oid = pending.pop()
            if oid in seen:
                continue
            seen.add(oid)
            obligation = obligations.get(oid, {})
            job = state["jobs"].get(obligation.get("source_job"), {})
            spec = job.get("spec", {})
            meta = spec.get("metadata", {})
            prior = state["inquiries"].get(meta.get("request_id"), {})
            if (job.get("state") != "applied" or spec.get("role") != "member" or meta.get("member_id") != member
                    or meta.get("topic_id") != scope["topic_id"] or meta.get("revision") != scope["revision"]
                    or meta.get("epoch") != scope["epoch"] or meta.get("topic_epoch", 0) != scope["topic_epoch"]
                    or prior.get("child") != scope["child"]):
                continue
            previous_scope = meta.get("transport_scope")
            if previous_scope != scope and (previous_scope is not None or scope["child"] is not None):
                continue
            for delivery in state["evidence_deliveries"]:
                if delivery.get("request_id") != prior.get("id") or delivery.get("member_id") != member:
                    continue
                for batch in delivery["batches"]:
                    if state["jobs"][batch["job_id"]]["state"] == "applied":
                        ranges.update(batch["material_ids"])
                pending.extend(delivery.get("responding_to", []))
        return ranges

    def _inquiry(self, con, state, topic, question, child=None, *, source_refs=(), source_text="", responding_to=()):
        if not question.strip():
            raise WorkflowError("consult requires a substantive inquiry")
        preparation_key = digest([topic, state["topics"][topic]["revision"], question, child, list(source_refs), source_text, list(responding_to)])
        resumed = next((r for r in state["inquiries"].values() if r.get("preparation_key") == preparation_key and r["state"] == "preparing"), None)
        rid = resumed["id"] if resumed else self._id(state, "REQ")
        current = state["topics"][topic]
        transport_scope = {"topic_id": topic, "revision": current["revision"], "epoch": state["epoch"],
            "topic_epoch": current.get("epoch", 0), "child": child,
            "child_revision": state["topics"][child]["revision"] if child else None,
            "child_epoch": state["topics"][child].get("epoch", 0) if child else None}
        catalog_sha = None
        obligation_context = {"unresolved_reference_ids": [x["id"] for x in state["obligations"] if x["topic_id"] == topic]}
        if state.get("version", 3) >= 4:
            members = [item["id"] for item in state["members"]]
            entries = []
            for item in state["obligations"]:
                if item["topic_id"] != topic:
                    continue
                job = state["jobs"].get(item.get("source_job"), {}).get("spec", {})
                owner = item.get("member_id") or (job.get("metadata", {}).get("member_id") if job.get("role") == "member" else None)
                audience = item.get("audience", ["*"])
                visible = [mid for mid in members if owner in {None, mid} and isinstance(audience, list)
                           and ({"*", mid} & set(audience)) and item.get("owner") != "USER"
                           and job.get("role") != "user_review" and job.get("metadata", {}).get("topic_id") != "USER"]
                entries.append({"id": item["id"], "state": item["state"], "visible_to": visible})
            snapshot = {"schema": "obligation_catalog.v1", "scope": transport_scope, "members": members, "entries": entries}
            catalog_sha = digest(snapshot)
            state.setdefault("obligation_catalogs", {}).setdefault(catalog_sha, snapshot)
            obligation_context = {"obligation_catalog": {"sha256": catalog_sha,
                "visible_counts": {mid: sum(mid in entry["visible_to"] for entry in entries) for mid in members},
                "retrieve": "obligation_catalog cursor=0 snapshot=" + catalog_sha,
                "request_via": "selection_requests.desired_difference",
                "page_size": 8, "boundary": "Sealed ID/state directory only, not obligation bodies or fulfillment; request subsequent pages by cursor with the same snapshot."}}
        inputs = [x for x in state["user_inputs"] if x.get("topic_id") in {None, topic, child}]
        tool_directory = None
        if state.get("version", 3) >= 4:
            tool_inputs = [x for x in inputs if x.get("kind") == "tool_result"]
            inputs = [x for x in inputs if x.get("kind") != "tool_result"]
            references = [
                {"id": x["id"], "kind": "tool_result_reference", "request_id": x["request_id"],
                 "source": x["source"], "topic_id": x["topic_id"], "outcome": x.get("value", {}).get("outcome"),
                 "body_delivered": False, "retrieve": "request exact INPUT id in inquiry/selection desired_difference"}
                for x in tool_inputs[-6:]]
            if references:
                # Factor repeated transport metadata without dropping a field
                # or changing the exact INPUT/TOOL locators or original bodies.
                common = {key: value for key, value in references[0].items()
                          if key not in {"id", "request_id"} and all(item[key] == value for item in references)}
                tool_directory = {"common": common,
                    "entries": [{key: value for key, value in item.items() if key not in common} for item in references]}
        # A single schema field can carry an explicit authored frame without
        # breaking stored jobs whose inquiry was ordinary prose.
        frame = None
        try:
            possible = json.loads(question)
            if isinstance(possible, dict) and set(possible) == {"situation", "observations", "interpretation", "global_question"}:
                if any(not isinstance(v, str) for v in possible.values()) or not possible["situation"].strip():
                    raise WorkflowError("structured inquiry requires text fields and an actual situation")
                frame = possible
        except json.JSONDecodeError:
            pass
        description = frame["situation"] if frame else question
        observations = encode({"latest_parent_summary": current["summary"], "user_inputs": inputs,
                               **({"author_observations": frame["observations"]} if frame else {}),
                               **obligation_context,
                               "responding_to": list(responding_to),
                               **({"tool_result_directory": tool_directory} if tool_directory is not None else {}),
                               "boundary": "Only actual A/B content is delivered evidence; referenced IDs alone are not originals. Separate batches preserve all requested source ranges."})
        if resumed is None:
            self._op(con, "request", id=rid, parent=topic, child=child, description=description,
                 observations=observations,
                 interpretation=(frame["interpretation"] or "当前解释未知") if frame else
                     (("父作者当前工作报告（可质疑，不等于事实）：" + current["summary"])
                     if current["summary"] else "当前尚无成形解释；允许先形成观察单位、比较对象和问题。"),
                 global_question=(frame["global_question"] or state["goal"]) if frame else state["goal"])
        request = self.parliament._get(con, rid, "request")
        inquiry = resumed or {"id":rid, "topic_id":topic, "child":child, "jobs":[], "state":"preparing", "candidates":[], "preparation_key":preparation_key}
        state["inquiries"][rid] = inquiry
        import re
        cursor_match = re.search(r"material_catalog\s+cursor=(\d+)", source_text + " " + question)
        catalog_cursor = int(cursor_match.group(1)) if cursor_match else 0
        source_notices = []
        for member in state["members"]:
            if any(state["jobs"][jid]["spec"]["metadata"].get("member_id") == member["id"] for jid in inquiry["jobs"]):
                continue
            prior_catalogs = set()
            for item in state["obligations"]:
                if item["id"] not in responding_to or item.get("member_id") != member["id"]:
                    continue
                spec = state["jobs"].get(item.get("source_job"), {}).get("spec", {})
                if spec.get("role") == "member" and spec.get("metadata", {}).get("member_id") == member["id"]:
                    previous = json.loads(spec["prompt"])
                    page_refs = set()
                    for material in previous["A"] + previous["B"]:
                        if "obligation_catalog" not in material.get("tags", []):
                            continue
                        provenance = json.loads(material["source"]["reference"])
                        match = re.fullmatch(r"OBLIGATION\.CATALOG\.([0-9a-f]{64})\.PAGE\.[0-9]+", provenance.get("id", ""))
                        if match:
                            page_refs.add(match[1])
                    prior = json.loads(previous["task"]["observations"]).get("obligation_catalog", {})
                    prior_catalogs.update(page_refs or ({prior["sha256"]} if prior.get("sha256") else set()))
            default_catalog = next(iter(prior_catalogs)) if len(prior_catalogs) == 1 else catalog_sha if not prior_catalogs else None
            sources = self._sources(con, state, references=source_refs, text=question + "\n" + source_text,
                                    member=member["id"], topic=topic, obligation_catalog_ref=default_catalog)
            required, manifest = self._source_materials(con, state, member["id"], sources, topic=topic, query=question)
            library = self._selection_library(con, state, member["id"], topic=topic, query=question)
            historical = set()
            denials = [{k: item[k] for k in ("id", "reason", "alternative") if k in item}
                       for item in manifest if item.get("state") == "denied"]
            if state.get("version", 3) >= 4:
                historical = self._inquiry_prior_ranges(state, transport_scope, member["id"], responding_to) & set(required)
                required = [mid for mid in required if mid not in historical] + [mid for mid in required if mid in historical]
            prefix_base = rid + "." + member["id"]
            strategy = "候选ID前缀(candidate_prefix)：" + prefix_base + ".B9999.C。证据批次9999/9999；仅评价本包实际可见范围；其他批次由父汇合。"
            if state.get("version", 3) >= 4:
                strategy = "候选ID前缀(candidate_prefix)：" + prefix_base + ".C。每次咨询仅生成一次；任务材料的已读范围限本包实际正文，一般知识和猜想另行注明。未入窗原件仍存在，需更多材料可按原件ID再次提出selection_requests；同一reselect链优先尚未递送片段。历史递送不进入本次上下文，不等于已读或采纳。"
                strategy += "\nSource transport: " + encode({"required_ranges": len(required),
                    "historical_ranges_not_inherited": len(historical), "unseen_before_call": len(required) - len(historical),
                    "denied_total": len(denials), "denied_sample": denials[:4],
                    "denial_boundary": "Denied originals exist but are not authorized here. Use the stated alternative; omitted denial details require a narrower explicit request."})
            limits = self.parliament._get(con, "CONFIG", "config")["limits"]
            try:
                select_input("member", member, request, library, required_ids=required, strategy=strategy, core=True, catalog_cursor=catalog_cursor, limits=limits)
            except SelectionBudgetError:
                required, library = self._prepare_council_sources(con, state, topic, member, required, library, limits)
            batches = [required]
            omitted_required = []
            delivered = []
            for index, batch in enumerate(batches):
                prefix = prefix_base + f".B{index + 1}.C" if required else prefix_base + ".C"
                final_strategy = "候选ID前缀(candidate_prefix)：" + prefix + f"。证据批次{index + 1}/{len(batches)}；仅评价本包可见范围；其他批次由父汇合。"
                if state.get("version", 3) >= 4:
                    prefix, final_strategy = prefix_base + ".C", strategy
                prompt, selection = select_input("member", member, request, library, required_ids=batch,
                    strategy=final_strategy, core=True, limits=limits,
                    offset=sum(1 for r in state["inquiries"].values() if r["topic_id"] == topic) - 1, catalog_cursor=catalog_cursor)
                pid = self._id(state, "PACKET")
                self._op(con, "packet", id=pid, member=member["id"], request=rid,
                         abstract_ids=selection["abstract_ids"], scenario_ids=selection["scenario_ids"],
                         strategy=selection["rendered_strategy"])
                sealed = self.parliament._get(con, pid, "packet")
                if sealed["prompt"] != prompt:
                    raise WorkflowError("selection and Parliament input sealing disagree")
                state["selections"].append({"packet": pid, "member": member["id"], "selection": selection})
                jid = self._job(state, "member", topic, prompt, {"request_id": rid, "packet_id": pid,
                    "member_id": member["id"], "candidate_prefix": prefix, "evidence_material_ids": batch,
                    "transport_scope": transport_scope,
                    "input_accounting": sealed["input_accounting"], "evidence_batch": index + 1, "evidence_batches": len(batches)})
                inquiry["jobs"].append(jid)
                delivered.append({"job_id": jid, "packet_id": pid, "material_ids": batch})
            state["evidence_deliveries"].append({"request_id": rid, "member_id": member["id"],
                "responding_to": list(responding_to), "source_manifest": manifest, "batches": delivered,
                "omitted_material_ids": omitted_required, "transport_scope": transport_scope,
                "historical_material_ids_not_inherited": sorted(historical),
                "state": "sealed_for_dispatch", "not_claimed": "model consumption or semantic adequacy"})
            if denials:
                source_notices.append({"member_id": member["id"], "denied_total": len(denials), "denied_sample": denials[:4]})
        inquiry["state"] = "generating"
        current["source_delivery_notice"] = {"request_id": rid, "members": source_notices,
            "boundary": "Current delivery denials, not missing originals. Full details remain in evidence_deliveries; scoped USER materials use their authorized entrance."}
        current["state"] = "consulting"

    def _prepare_council_sources(self, con, state, topic, member, required, library, limits):
        by_id = {m["id"]:m for m in library}
        originals = [{"id":mid, "content":by_id[mid]["content"], "source":by_id[mid]["source"]} for mid in required]
        text = encode(originals)
        target = min(limits["scenario_chars"] * 2 // 3, context_policy(state)["summary_target_chars"])
        prepared, units, assurance = self._compact_text(state, topic, "council:" + member["id"], text, target,
            fits=lambda body: len(body) <= target)
        if len(prepared) > target or not units:
            raise SelectionBudgetError("requested sources cannot be faithfully prepared for the council window; originals retained, explicit restructuring required", {"source_chars":len(text), "target_chars":target})
        mid = "PREPARED." + digest([member["id"], required, units, prepared])[:24]
        inherited = sorted({tag for material_id in required for tag in by_id[material_id].get('tags', [])
                            if tag in {'user_collaboration','profile_only'}})
        if not con.execute("SELECT 1 FROM objects WHERE id=?", (mid,)).fetchone():
            self._op(con, "material", id=mid, owner=member["id"], content=prepared, window="scenario",
                source={"kind":"observation", "reference":encode({"source_material_ids":required,"memory_unit_ids":units,
                    "assurance":assurance,"source_sha256":digest(text),"representation":"reviewed semantic preparation, not original reading"})},
                refs=required, tags=["prepared_evidence", "runtime_original", *inherited])
        library = [m for m in library if m['id'] != mid] + [self.parliament._get(con, mid, "material")]
        state.setdefault("council_preparations", {})[mid] = {"member_id":member["id"],"topic_id":topic,
            "source_material_ids":required,"memory_unit_ids":units,"source_sha256":digest(text),"assurance":assurance}
        return [mid], library

    def _pending(self, state, topic=None):
        return [j for j in state["jobs"].values() if j["state"] == "pending" and
                (topic is None or j["spec"]["metadata"]["topic_id"] == topic)]

    def _scope(self, state, topic):
        scope = {topic}
        while True:
            expanded = scope | {t["id"] for t in state["topics"].values() if t["parent"] in scope}
            if expanded == scope:
                return scope
            scope = expanded

    def _topic_task(self, con, state, topic):
        current = state["topics"][topic]
        summary_source = None
        decision = current.get("last_decision", {})
        source_job = state["jobs"].get(decision.get("job_id"), {})
        source_spec = source_job.get("spec", {})
        source_meta = source_spec.get("metadata", {})
        result_id = decision.get("source_result")
        if (source_job.get("state") == "applied" and source_spec.get("role") == "parent"
                and source_meta.get("topic_id") == topic and source_job.get("result_id") == result_id):
            source_kind = None
            row = con.execute("SELECT body,outcome FROM results WHERE result_id=?", (result_id,)).fetchone()
            if row and row[1] == "applied":
                original = json.loads(row[0])
                if original.get("payload", {}).get("summary") == current["summary"]:
                    source_kind = _result_source_kind(original)
            elif row is None:
                # Immediate successors are sealed in this transaction before the
                # result receipt is inserted. This exact marker commits or rolls
                # back together with the applied job, summary and successor.
                marker = decision.get("summary_source", {})
                if marker.get("sha256") == digest(current["summary"]):
                    source_kind = marker.get("report_source_kind")
            if source_kind in {"fixture", "model"}:
                summary_source = {"source_job": decision["job_id"], "result_id": result_id,
                    "revision": source_meta["revision"], "epoch": source_meta["epoch"],
                    "topic_epoch": source_meta.get("topic_epoch", 0), "status": "applied",
                    "summary_sha256": digest(current["summary"]), "report_source_kind": source_kind,
                    "boundary": "This exact prior role report is explicit current context with an attributable source. It is not a new reading, tool execution, or independent validation of its underlying evidence."}
        return {"goal": state["goal"], "topic": topic,
                "revision": self.parliament._get(con, current["revision"], "revision"),
                "prior_summary": current["summary"],
                "dependencies": self._dependency_context(state, topic),
                **({"prior_summary_source": summary_source} if summary_source else {}),
                **({"source_delivery_notice": current["source_delivery_notice"]} if current.get("source_delivery_notice", {}).get("members") else {}),
                "user_inputs": [x for x in state["user_inputs"] if x.get("topic_id") in {None, topic}],
                "questions": [x for x in state["questions"].values() if x["topic_id"] == topic],
                "unresolved": [x for x in state["obligations"] if x["topic_id"] == topic],
                "tool_recoveries": [x for x in state.get("tool_recoveries", []) if x["topic_id"] == topic
                                    and x["state"] in {"awaiting_action","retry_requested"}],
                "tool_requests": [x for x in state["tool_requests"] if x["topic_id"] == topic]}

    def _work_history(self, con, state, topic, role, *, archived=False):
        """Only this role/topic's accepted public outputs, never peer memories."""
        history = []
        cached = {} if archived else {row[0]:json.loads(row[1]) for row in con.execute(
            'SELECT job_id,body FROM work_reports WHERE topic=? AND role=?', (topic,role))}
        for job in sorted(state["jobs"].values(), key=lambda j: (j.get("created_at", ""),
                int(j["spec"].get("job_id", "0").rsplit(".",1)[-1]) if j["spec"].get("job_id", "0").rsplit(".",1)[-1].isdigit() else 0)):
            spec = job["spec"]
            if job["state"] != "applied" or spec["role"] != role or spec["metadata"]["topic_id"] != topic:
                continue
            if spec['job_id'] in cached:
                history.append(cached[spec['job_id']]); continue
            row = con.execute("SELECT body,outcome FROM results WHERE result_id=?", (job.get("result_id"),)).fetchone()
            if not row or row[1] != "applied":
                continue
            result = json.loads(row[0])
            record = {"job_id":spec["job_id"],"result_id":job["result_id"],"role":role,
                "revision":spec["metadata"]["revision"],"created_at":job["created_at"],
                "output":result["payload"],
                "boundary":"Accepted historical role output, not a current instruction, current tool execution or proof of factual correctness."}
            if not archived:
                record = working_history([record])[0]
                con.execute('INSERT OR IGNORE INTO work_reports VALUES(?,?,?,?)', (spec['job_id'],topic,role,encode(record)))
            history.append(record)
        return history if archived else project_history(history)

    def _archive_work_report(self, con, state, spec, result):
        """Commit the small semantic projection with the original result.

        Artifact contents and transport payloads remain in immutable originals;
        subsequent role turns do not repeatedly deserialize those large bodies.
        """
        record = working_history([{'job_id':spec['job_id'],'result_id':result['result_id'],
            'role':spec['role'],'revision':spec['metadata']['revision'],
            'created_at':state['jobs'][spec['job_id']]['created_at'],'output':result['payload'],
            'boundary':'Accepted historical role output, not current instructions or independent evidence.'}])[0]
        con.execute('INSERT INTO work_reports VALUES(?,?,?,?)',
            (spec['job_id'],spec['metadata']['topic_id'],spec['role'],encode(record)))

    def _prior_tool_result_delivery(self, state, role, topic, inputs):
        # A tool result becomes optional history only after its exact body was
        # included in a successfully applied call of this same role and scope.
        # Failed, stale, foreign-topic and reference-only inputs prove nothing.
        current = state["topics"][topic]
        inputs = {item["id"]: digest(item) for item in inputs if item.get("kind") == "tool_result"}
        delivered = {}
        for job in sorted(state["jobs"].values(), key=lambda j: (j.get("applied_at", j.get("created_at", "")), j["spec"]["job_id"])):
            spec, meta = job["spec"], job["spec"]["metadata"]
            if (not inputs or job["state"] != "applied" or spec["role"] != role
                    or meta.get("topic_id") != topic or meta.get("revision") != current["revision"]
                    or meta.get("epoch") != state["epoch"] or meta.get("topic_epoch", 0) != current.get("epoch", 0)):
                continue
            receipts = meta.get('sealed_tool_inputs')
            if receipts is None:
                receipts = [{'input_id':i['id'],'input_sha256':digest(i)} for i in json.loads(spec['prompt'])['task'].get('user_inputs', []) if i.get('kind') == 'tool_result']
            for item in receipts:
                if inputs.get(item['input_id']) == item['input_sha256']:
                    delivered.pop(item['input_id'], None)
                    delivered[item['input_id']] = {**item,'job_id':spec['job_id']}
        return list(delivered.values())

    def _project(self, state, role, task, schema, *, max_chars=None, reserve_tokens=0, reserve_chars=0):
        if state.get("version", 3) < 4:
            return ordinary_input(role, task, schema=schema), {}
        from .context import project_context
        from .tool_adapters import LocalToolAdapter
        repair = state["topics"][task["topic"]].get("pending_role_repair", {}).get(role)
        if repair:
            task["input_rebuild" if repair.get("kind") == "input_contract" else "validation_failure"] = deepcopy(repair)
        # Share administrative completion within the responsible scope only.
        scope = self._scope(state, task["topic"])
        relevant = [j for j in state["jobs"].values() if j["spec"]["role"] in {"memory_review", "user_review", "processing"}
                    and j["spec"]["metadata"]["topic_id"] in scope]
        task["cognitive_status"] = {"recent": [{"job_id": j["spec"]["job_id"], "role": j["spec"]["role"],
            "state": j["state"], "result_id": j.get("result_id"),
            "operation": j["spec"]["metadata"].get("operation_packet", {}).get("operation")}
            for j in relevant[-8:]], "total": len(relevant),
            "boundary": "Execution/import status, not private content or cognitive benefit. Older records: cognition.status(job_id)."}
        if not relevant:
            task.pop("cognitive_status")
        delivered = self._prior_tool_result_delivery(state, role, task["topic"], task.get("user_inputs", []))
        if delivered:
            task["prior_tool_result_delivery"] = delivered
        # Current evidence is selected by actual event provenance, not by
        # repeatedly filling unused context with all archived bodies.
        prior = state["topics"][task["topic"]].get("last_decision", {}).get("job_id")
        focus = {state["return_sources"].get(r["id"]) for r in task.get("new_returns", [])}
        focus.update(x.get("source_job") for x in task.get("user_inputs", [])
                     if state.get("input_receipts", {}).get(x["id"], {}).get("state") != "processed")
        if role == "author":
            latest = [j["spec"]["job_id"] for j in state["jobs"].values() if j["state"] == "applied"
                      and j["spec"]["role"] == role and j["spec"]["metadata"]["topic_id"] == task["topic"]]
            focus.update(latest[-1:])
        if not any(j["state"] == "applied" and j["spec"]["role"] == role and j["spec"]["metadata"]["topic_id"] == task["topic"] for j in state["jobs"].values()):
            focus.update(a["source_job"] for a in task.get("artifacts", []))
        task["context_focus_jobs"] = sorted(focus - {None})
        task["tool_catalog"] = LocalToolAdapter(self.path, enable_sandbox_tests=bool(state["metadata"].get("sandbox_tests"))).catalog()
        from .web_research import catalog as web_catalog
        task["tool_catalog"].extend(web_catalog(state["metadata"].get("web_search", "live")))
        self._file_context(state, task)
        task["tool_catalog"].append({"kind": "web.read", "description": "取回本议题或其子题的实际检索原件；arguments={request_id,start?:0,max_chars?:1..8000}。按原文范围返回，无新检索；目录与来源不等于事实已验证。"})
        task["tool_catalog"].append({"kind": "context.retrieve", "description": "Read an original from this call's context_projection using reference{source_sha256,pointer},start,max_chars (max4000); context.catalog uses cursor/page_size, optional section=artifacts for path/source_job/hash directory pages. For files generated in this response use path @self/name."})
        task["tool_catalog"].append({"kind": "user.consult", "description": "Ask the fixed USER responsibility for conditional collaboration guidance; arguments={need:string}. Await actual review. Not required for routine clarification."})
        from .cognitive_operations import tool_catalog
        task["tool_catalog"].extend(tool_catalog())
        task["tool_catalog"].extend([
            {"kind":"history.catalog", "description":"列出本角色、本议题已采纳的公开历史输出；arguments={cursor:0}，每页20条。"},
            {"kind":"history.read", "description":"读取本角色本议题已采纳输出的原文（包括no_change折叠项）；arguments={job_id,start:0,max_chars:16000}。保留实际返回范围，不继承其他角色记忆。"}])
        task["tool_catalog"].append({"kind":"cognition.status", "description":"读取本责任范围某次认知/经验/USER 调用的执行和导入状态，不读取私有正文；arguments={job_id}。完成不证明认知收益。"})
        settings = context_policy(state)
        settings = {**settings, "platform_reserve_tokens": settings["platform_reserve_tokens"] + reserve_tokens}
        def fits(prompt, contract, images):
            measured = capacity(prompt, contract, images=images, settings=settings)
            pressure = state["topics"][task["topic"]].get("context_pressure", {})
            return (measured["fits"] and measured["remaining_prompt_chars"] >= reserve_chars
                    and len(prompt) <= pressure.get("max_prompt_chars", measured["max_prompt_chars"]))
        # The CLI character boundary and model token capacity are independent;
        # neither is an attention window or a reason to compress fitting text.
        from .context_budget import CLI_MAX_INPUT_CHARS
        max_chars = min(max_chars or CLI_MAX_INPUT_CHARS, CLI_MAX_INPUT_CHARS)
        pending_inputs = set(self._input_obligations(state, task))
        task["input_obligations"] = self._input_obligations(state, task)
        pressure = state["topics"][task["topic"]].get("context_pressure", {})
        # A known character bound needs lossless projection first, not an
        # arbitrary 25% token reduction and unnecessary semantic preparation.
        force = bool(pressure) and pressure.get("boundary") != "cli_input_chars"
        attempted_compaction = False
        while True:
            if set(self._input_obligations(state, task)) != pending_inputs or set(task["input_obligations"]) != pending_inputs:
                raise InputContractError("context preparation changed outstanding input obligations")
            try:
                if force and not attempted_compaction:
                    raise ProjectionBudgetError("provider reported capacity pressure")
                bundle = project_context(role, task, max_chars=max_chars, schema=schema, capacity_check=fits)
                if role == "author" and len(bundle["batches"]) != 1:
                    raise ProjectionBudgetError("author requires all current decision groups")
                break
            except ProjectionBudgetError:
                if attempted_compaction:
                    raise
                attempted_compaction = True
                prepared = self._prepare_context(state, role, task, force=True, schema=schema)
                if prepared == task:
                    raise ProjectionBudgetError("complete required input exceeds capacity; originals retained for explicit paging",
                        accounting=capacity(ordinary_input(role, task, schema=schema), schema, settings=settings))
                task = prepared
        state["context_snapshots"][bundle["source_sha256"]] = task
        first = bundle["batches"][0]
        first["manifest"]["max_chars"] = bundle["max_chars"]
        return first["prompt"], {**first["manifest"], "task": first["task"]} if role == "parent" else first["manifest"]

    def context_result(self, request):
        from .context import retrieve_context, context_catalog
        with self._tx() as (con, state):
            saved = next((r for r in state["tool_requests"] if r["id"] == request["id"]), None)
            if saved != request:
                raise WorkflowError("context request must match the persisted original")
            spec = state["jobs"][request["source_job"]]["spec"]
            args = request["arguments"]
            if request["kind"] == "context.retrieve" and "path" in args:
                if "reference" in args or not isinstance(args["path"], str):
                    raise WorkflowError("generated file retrieval requires a single explicit path")
                row = con.execute("SELECT path,content,source_job FROM artifacts WHERE path=?", (args["path"],)).fetchone()
                if row is None or row[2] != spec["job_id"]:
                    raise WorkflowError("generated original is outside the producing call's scope")
                artifact = dict(path=row[0],content=row[1],source_job=row[2])
                original = {"generated_artifact":artifact}
                sha = digest(original)
                state["context_snapshots"][sha] = original
                value = retrieve_context(original, {"source_sha256":sha,"pointer":"/generated_artifact"},
                    start=args.get("start",0), max_chars=min(args.get("max_chars",4000),4000))
                return {"outcome":"succeeded","payload":value,"receipt":{"adapter":"sealed_context",
                    "source_job":spec["job_id"],"source_sha256":sha,"generated_path":row[0]}}
            manifest = spec["metadata"].get("context_projection", {})
            sha = manifest.get("source_sha256")
            task = json.loads(spec["prompt"])["task"]
            semantic = task.get("semantic_memory", {})
            allowed = {sha, semantic.get("original_source", {}).get("source_sha256"),
                       task.get("history_memory", {}).get("source", {}).get("source_sha256")}
            allowed.update(x["reference"]["source_sha256"] for x in semantic.get("replacements", []))
            requested_sha = request["arguments"].get("reference", {}).get("source_sha256")
            if requested_sha and requested_sha in allowed:
                sha = requested_sha
            if not sha or sha not in state["context_snapshots"]:
                raise WorkflowError("request has no authorized original context")
            original = state["context_snapshots"][sha]
            args = request["arguments"]
            if request["kind"] == "context.catalog":
                if "reference" in args and args["reference"].get("source_sha256") != sha:
                    raise WorkflowError("context source is outside the requesting role's scope")
                value = context_catalog(original, cursor=args.get("cursor", 0), page_size=min(args.get("page_size", 8), 12), section=args.get("section"))
                if "reference" in args and args["reference"] != value.get("reference", {"source_sha256": sha, "pointer": ""}):
                    raise WorkflowError("catalog reference does not identify the requested section")
            else:
                ref = args.get("reference", {})
                if ref.get("source_sha256") != sha:
                    raise WorkflowError("context source is outside the requesting role's scope")
                value = retrieve_context(original, ref, start=args.get("start", 0), max_chars=min(args.get("max_chars", 4000), 4000))
            return {"outcome": "succeeded", "payload": value, "receipt": {"adapter": "sealed_context", "source_job": spec["job_id"], "source_sha256": sha}}

    def user_guidance_result(self, request):
        from . import user_collaboration as user
        with self._tx() as (con, state):
            eid = request["id"] + ".CONSULT"
            if not any(e["id"] == eid for e in state["user_collaboration"]["events"]):
                need = request["arguments"].get("need", "")
                if not isinstance(need, str) or not need.strip() or len(need) > 6000:
                    return {"outcome": "failed", "error": "user.consult needs a bounded explicit need"}
                user.record_event(state, eid, "review_requested", source="model", topic_id=request["topic_id"],
                    job_id=request["source_job"], content=need, allowed_uses=["profile", "collaboration"], audience=["*"])
                self._user_review(con, state)
                return None
            if any(eid in b["event_ids"] and b["state"] == "quarantined" for b in state["user_collaboration"].get("quarantined_batches", [])):
                return {"outcome":"failed", "error":"USER review batch is quarantined; independent observations continue. Explicit review retry remains available.",
                        "receipt":{"adapter":"user_collaboration", "event_id":eid,"executed":False}}
            if eid not in state["user_collaboration"]["processed_event_ids"]:
                return None
            material = user.select_materials(state, purpose="collaboration", audience="author:" + request["topic_id"],
                topic_id=request["topic_id"], query=request["purpose"], max_chars=4000)
            return {"outcome": "succeeded", "payload": material, "receipt": {"adapter": "user_collaboration", "event_id": eid,
                "boundary": "Conditional model-authored guidance, not proof of user benefit."}}

    def _author(self, con, state, topic):
        task = self._topic_task(con, state, topic)
        if state.get("version", 3) >= 4:
            task["interaction"] = self._interaction_context(state, topic, "author")
        task["working_state"] = self._working_context(state, topic, "author")
        task["work_history"] = self._work_history(con, state, topic, "author")
        task["I"] = [self.parliament._get(con, mid, "material") for mid in task["revision"]["I"]]
        if state.get("version", 3) >= 4:
            from . import user_collaboration as user
            task["collaboration_guidance"] = user.select_materials(state, purpose="collaboration", audience="author:" + topic,
                topic_id=topic, query=encode(task["revision"]["questions"]), max_chars=3000, max_items=4)
        task["public_evidence"] = self._public_evidence(con, state, topic, 'author')
        schema = runtime_schema("author", v4=state.get("version", 3) >= 4)
        prompt, projection = self._project(state, "author", task, schema)
        jid = self._job(state, "author", topic, prompt, {"context_projection": projection}, schema=schema)
        state["topics"][topic]["state"] = "working"
        state["topics"][topic]["author_refresh_required"] = False
        return jid

    def _parent(self, con, state, topic):
        current = state["topics"][topic]
        owned_scope = self._scope(state, topic)
        inquiry_ids = [r["id"] for r in state["inquiries"].values() if r["topic_id"] == topic and r["state"] == "ready"]
        candidates = [self.parliament._get(con, cid, "candidate") for r in inquiry_ids
                      for cid in state["inquiries"][r]["candidates"] if cid not in state["inquiries"][r].get("disposed", [])]
        task = self._topic_task(con, state, topic)
        from .feedback import parent_ready
        task["tool_recoveries"] = [deepcopy(d) for d in state.get("tool_recoveries", [])
            if d["topic_id"] in owned_scope and d["state"] in {"awaiting_action","retry_requested"}]
        if state.get("version", 3) >= 4:
            task["interaction"] = self._interaction_context(state, topic, "parent")
        parent_ready(state, topic)
        task["feedback_batch"] = deepcopy(current.get("feedback_wait", {}))
        task["feedback_notices"] = [deepcopy(n) for n in current.get("feedback_notices", []) if not n.get("handled")]
        task["working_state"] = self._working_context(state, topic, "parent")
        task["work_history"] = self._work_history(con, state, topic, "parent")
        task["public_evidence"] = self._public_evidence(con, state, topic, 'parent')
        task.update(undisposed_candidates=candidates,
                    requests=[self.parliament._get(con, r, "request") for r in inquiry_ids],
                    inquiry_results=[{"id":r, "outcomes":deepcopy(state["inquiries"][r].get("outcomes", [])),
                        "boundary":"Candidate generation results only. No candidates is not a completed audit. Use bounded_review with explicit originals/checklist for finite verification."}
                        for r in inquiry_ids if not state["inquiries"][r].get("uptake")],
                    new_returns=[{**self.parliament._get(con, r, "return"),
                        "delivery_kind":state.get("return_kinds", {}).get(r,"complete")}
                        for r in current["new_returns"]],
                    assessments=[a for a in self.parliament._all(con, "assessment") if a["returned"] in current["new_returns"]],
                    children=[t for t in state["topics"].values() if t["parent"] == topic],
                    active_jobs=[{"id": j["spec"]["job_id"], "role": j["spec"]["role"],
                                  "topic_id": j["spec"]["metadata"]["topic_id"]} for j in self._pending(state)
                                  if j["spec"]["metadata"]["topic_id"] in owned_scope
                                  and j["spec"]["role"] not in {"memory_review","user_review"}],
                    artifacts=[{k:v for k,v in a.items() if k not in {'id','result_id'}} for a in task['public_evidence']['artifacts']],
                    selection_requests=[o for o in state["obligations"] if o.get("kind") == "selection" and o["topic_id"] == topic and o["state"] == "open"],
                    escalations=[{**e, "candidate": self.parliament._get(con, e["candidate_id"], "candidate")}
                                 for e in state["escalations"] if e["receiver"] == topic and e["state"] == "pending"],
                    escalation_outcomes=[e for e in state["escalations"] if e["topic_id"] == topic],
                    late_originals=[{**r, "original": json.loads(con.execute("SELECT body FROM results WHERE result_id=?", (r["result_id"],)).fetchone()[0])}
                                    for r in state["late_results"] if r.get("state") == "pending"
                                    and state["jobs"].get(r["job_id"], {}).get("spec", {}).get("metadata", {}).get("topic_id") in owned_scope],
                    existing_substitutes=[{"id": o["id"], "kind": o["kind"], "q": o.get("q"), "questions": o.get("questions")}
                                          for kind in ("candidate", "revision") for o in self.parliament._all(con, kind)])
        # Historical source material is visible for reasoning, but it is not a
        # second authorization to commit the same contribution disposition.
        original_task = deepcopy(task)
        # Start with the actual final cap. Scope enums are measured below and
        # trigger exact repacking; a fixed reserve can strand a valid group.
        from .context_budget import CLI_MAX_INPUT_CHARS
        packing_limit = CLI_MAX_INPUT_CHARS
        final_limit = packing_limit
        scope_reserve_tokens = 0
        scope_reserve_chars = 0
        while True:
            task = deepcopy(original_task)
            schema = runtime_schema("parent", v4=state.get("version", 3) >= 4)
            _, projection = self._project(state, "parent", task, schema, max_chars=packing_limit,
                reserve_tokens=scope_reserve_tokens, reserve_chars=scope_reserve_chars)
            if projection:
                task = projection.pop("task")
            candidates = task["undisposed_candidates"]
            current_returns = [r["id"] for r in task["new_returns"]]
            if current_returns:
                # SCHEMAS shares its primitive string nodes. deepcopy preserves
                # those aliases; replace this field rather than mutating the node.
                schema["properties"]["contribution_decisions"]["items"]["properties"]["return_id"] = {
                    "type": "string", "enum": current_returns}
            scopes = {"dispositions": ("candidate_id", [c["id"] for c in candidates]),
                      "contribution_decisions": ("return_id", current_returns),
                      "selection_responses": ("request_id", [o["id"] for o in task["selection_requests"]]),
                      "escalation_responses": ("disposition_id", [o["disposition_id"] for o in task["escalations"]])}
            if state.get("version",3) >= 4:
                scopes["inquiry_uses"] = ("request_id", [r["id"] for r in task.get("inquiry_results", [])])
            for field, (key, ids) in scopes.items():
                if ids:
                    schema["properties"][field]["items"]["properties"][key] = {"type": "string", "enum": ids}
                else:
                    schema["properties"][field]["maxItems"] = 0
            task["response_scope"] = {**{field + "_exact_ids": ids for field, (_, ids) in scopes.items()},
                "rule": "Process exactly these IDs once in each response array. An empty list requires []. IDs mentioned in historical summaries or unresolved records do not belong to this batch. Omitted obligations will be supplied in later batches; do not repeat them here."}
            prompt = ordinary_input("parent", task, schema=schema)
            projected_limit = projection.get("max_chars", packing_limit)
            final_limit = max(final_limit, projected_limit)
            actual_limit = final_limit
            if state.get("version", 3) < 4 or capacity(prompt, schema, images=len(task.get("native_images", [])), settings=context_policy(state))["fits"]:
                break
            measured = capacity(prompt, schema, images=len(task.get("native_images", [])), settings=context_policy(state))
            if not measured["token_fits"]:
                scope_reserve_tokens += max(1024, -measured["remaining_text_tokens"] + 1024)
            if not measured["character_fits"]:
                scope_reserve_chars += -measured["remaining_prompt_chars"] + 2048
            if scope_reserve_tokens >= measured["available_text_tokens"]:
                raise ProjectionBudgetError("final parent contract cannot fit", accounting=measured)
            if scope_reserve_chars >= measured["max_prompt_chars"]:
                raise ProjectionBudgetError("final parent contract exceeds CLI character boundary", accounting=measured)
        if state.get("version", 3) >= 4:
            projection["explicit_chars"] = len(prompt)
            projection["max_chars"] = actual_limit
        jid = self._job(state, "parent", topic, prompt,
                        {"request_ids": inquiry_ids, "candidate_ids": [c["id"] for c in candidates],
                         "return_ids": current_returns, "context_projection": projection}, schema=schema)
        state["jobs"][jid]["spec"]["metadata"]["selection_ids"] = [o["id"] for o in task["selection_requests"]]
        state["jobs"][jid]["spec"]["metadata"]["inquiry_result_ids"] = [r["id"] for r in task.get("inquiry_results", [])]
        state["jobs"][jid]["spec"]["metadata"]["late_ids"] = [o["result_id"] for o in task["late_originals"]]
        state["jobs"][jid]["spec"]["metadata"]["escalation_ids"] = [o["disposition_id"] for o in task["escalations"]]
        from .feedback import seal_feedback
        state["jobs"][jid]["spec"]["metadata"]["feedback_batch"] = seal_feedback(state, topic, jid)
        state["jobs"][jid]["spec"]["metadata"]["feedback_notice_ids"] = [n["source_job"] for n in task.get("feedback_notices", [])]
        current["dirty"] = False
        for rid in inquiry_ids:
            state["inquiries"][rid]["state"] = "deciding"
        return jid

    def _budgeted_schedule(self, con, state, stage, operation):
        """Isolate one scheduling attempt, catching only explicit input limits.

        Valid imported semantics and earlier scheduled siblings stay committed.
        Shape, permission and semantic violations still propagate to the caller.
        """
        before = StateStore(self.db, con).pack(state)
        con.execute("SAVEPOINT bounded_scheduling")
        try:
            operation()
        except ContextPending:
            # The preparation jobs and original sources are durable progress.
            # Keep the business scheduling intent pending, and never roll them back.
            con.execute("RELEASE bounded_scheduling")
            return True
        except (ProjectionBudgetError, SelectionBudgetError, InputContractError) as exc:
            con.execute("ROLLBACK TO bounded_scheduling")
            con.execute("RELEASE bounded_scheduling")
            state.clear(); state.update(StateStore(self.db, con).decode(before))
            detail = {"stage": stage, "error_type": type(exc).__name__, "error": str(exc),
                      "reference": getattr(exc, "reference", None), "chars": getattr(exc, "chars", None),
                      "max_chars": getattr(exc, "max_chars", None), "accounting": getattr(exc, "accounting", None)}
            key = digest(detail)
            kind = "input_contract_blocked" if isinstance(exc, InputContractError) else "input_budget_blocked"
            if not any(d.get("kind") == kind and d.get("signature") == key and d.get("status") == "blocked"
                       for d in state["diagnostics"]):
                state["diagnostics"].append({"kind": kind, "signature": key, "status": "blocked", **detail,
                    "pending_schedule_ids": [i["id"] for i in state["schedule_intents"] if i["state"] == "pending"],
                    "action": "Explicitly restructure the named input or pending task; then advance. Received results remain applied; do not rerun the tool or resample the valid output.",
                    "boundary": "Only this scheduling action was rolled back; independent scheduled jobs survive. No evidence was truncated."})
            state["state"] = "needs_attention"
            state["state_reason"] = "follow-up input requires explicit restructuring; received results are preserved: " + str(exc)
            return False
        else:
            con.execute("RELEASE bounded_scheduling")
            for diagnostic in state["diagnostics"]:
                if (diagnostic.get("kind") in {"input_budget_blocked", "input_contract_blocked"} and diagnostic.get("status") == "blocked"
                        and diagnostic.get("stage") == stage):
                    diagnostic["status"] = "scheduling_resumed"
            return True

    def _progress(self, con, state):
        self._dependency_progress(state)
        for delivery in state["evidence_deliveries"]:
            jobs = [state["jobs"][batch["job_id"]] for batch in delivery["batches"]]
            if jobs and all(j["state"] == "applied" for j in jobs):
                delivery["state"] = "fresh_results_received"
                delivery["result_ids"] = [j["result_id"] for j in jobs]
                delivery["not_claimed"] = "Semantic adequacy and attention to each field are authored judgments, not transport facts."
                inquiry = state['inquiries'].get(delivery.get('request_id'), {})
                if inquiry.get('uptake'):
                    delivery['state'] = 'parent_uptake_recorded'
                    delivery['uptake_job_ids'] = [u['job_id'] for u in inquiry['uptake']]
            elif jobs and any(j['state']=='superseded' for j in jobs):
                delivery['state'] = 'superseded'
                delivery['not_claimed'] = 'A required recipient call was superseded; partial originals remain history, not current uptake.'
            elif jobs and any(j['state']=='failed' for j in jobs):
                delivery['state'] = 'recipient_failed'
                delivery['not_claimed'] = 'Actual recipient failure; no processing is inferred. Retry follows the existing saved call.'
        for obligation in state['obligations']:
            if obligation.get('kind') != 'selection' or obligation['state'] not in {'delivery_prepared','consumer_processed','delivery_acknowledged'}:
                continue
            receipts = obligation.get('delivery_receipts', [])
            delivered = [d for d in state['evidence_deliveries'] if obligation['id'] in d.get('responding_to', [])]
            if receipts and delivered and all(d['state'] in {'fresh_results_received','parent_uptake_recorded'} for d in delivered):
                obligation['state'] = 'delivery_acknowledged' if all(d['state'] == 'parent_uptake_recorded' for d in delivered) else 'consumer_processed'
                obligation['delivery_claim'] = 'Recipients returned actual results' + (' and parent recorded use/effect.' if obligation['state'] == 'delivery_acknowledged' else '; parent uptake still pending.') + ' This does not certify adequacy or close any newly raised request.'
        if state["paused"]:
            return
        complete = self._drain_schedule_intents(con, state)
        if state.get("version", 3) >= 4:
            complete = self._budgeted_schedule(con, state, "user_review", lambda: self._user_review(con, state)) and complete
            complete = self._budgeted_schedule(con, state, "memory_review", lambda: self._memory_review(con, state)) and complete
        for inquiry in state["inquiries"].values():
            if inquiry["state"] == "generating" and all(state["jobs"][j]["state"] == "applied" for j in inquiry["jobs"]):
                inquiry["state"] = "ready"
                state["topics"][inquiry["topic_id"]]["dirty"] = True
        # A returned author has independent feedback obligations. Another topic's
        # pending author never blocks these assessments or its parent's turn.
        for topic in list(state["topics"].values()):
            for returned in topic.get("awaiting_feedback", []):
                assessment_jobs = [j for j in state["jobs"].values() if j["spec"]["metadata"].get("returned") == returned and j["state"] != "superseded"]
                intents = [i for i in state['schedule_intents'] if i['kind']=='assessments'
                           and i['arguments'].get('returned')==returned and i['state']!='superseded']
                assessment_ready = (bool(intents) and all(i['state']=='scheduled' for i in intents)) if state.get('version',3)>=4 else bool(assessment_jobs)
                if assessment_ready and all(j["state"] == "applied" for j in assessment_jobs):
                    parent = state["topics"][topic["parent"]]
                    if returned not in parent["new_returns"] and returned not in parent["consumed_returns"]:
                        parent["new_returns"].append(returned)
                        parent["dirty"] = True
                    source = state["jobs"].get(state["return_sources"].get(returned), {}).get("spec", {})
                    # A partial result remains useful to the parent without
                    # erasing its author's wait or a later version's live work.
                    if (state.get("return_kinds", {}).get(returned, "complete") == "complete"
                            and source.get("metadata", {}).get("revision") == topic["revision"]
                            and returned == topic.get("awaiting_feedback", [])[-1]
                            and topic["state"] == "assessing"):
                        topic["state"] = "returned"
        for repair in state["reassessments"].values():
            if repair["state"] == "pending" and all(state["jobs"][j]["state"] == "applied" for j in repair["jobs"]):
                returned = self.parliament._get(con, repair["returned"], "return")
                parent = state["topics"][state["topics"][returned["topic"]]["parent"]]
                if returned["id"] not in parent["new_returns"]:
                    parent["new_returns"].append(returned["id"])
                parent["dirty"] = True
                repair["state"] = "fresh_assessment_received"
        for topic_id in list(state["topics"]):
            topic = state["topics"][topic_id]
            if topic.get("fixed"):
                continue
            own = self._pending(state, topic_id)
            failed_author = any(j["state"] == "failed" and j["spec"]["role"] == "author" and j["spec"]["metadata"]["topic_id"] == topic_id for j in state["jobs"].values())
            if topic.get("author_refresh_required") and not own and not failed_author:
                def refresh_author():
                    self._author(con, state, topic_id)
                    state["topics"][topic_id]["author_refresh_required"] = False
                complete = self._budgeted_schedule(con, state, "author_refresh:" + topic_id, refresh_author) and complete
            topic = state["topics"][topic_id]
            own = self._pending(state, topic_id)
            failed_control = any(j["state"] == "failed" and j["spec"]["metadata"]["topic_id"] == topic_id and
                                 j["spec"]["role"] in {"parent", "member"} for j in state["jobs"].values())
            pending_intent = any(i["state"] == "pending" and i["topic_id"] == topic_id for i in state["schedule_intents"])
            from .feedback import parent_ready
            if topic["dirty"] and parent_ready(state, topic_id) and not failed_control and not pending_intent and not any(j["spec"]["role"] in {"author", "parent", "member"} for j in own):
                complete = self._budgeted_schedule(con, state, "parent:" + topic_id,
                    lambda: self._parent(con, state, topic_id)) and complete
        if not complete:
            state["state"] = "needs_attention"
            state["state_reason"] = "some follow-up inputs require explicit restructuring; received results and independent queued jobs are preserved"
        elif self._pending(state):
            state["state"] = "active"
            state["state_reason"] = "durable role jobs or semantic duties are pending"
        elif any(j["state"] == "failed" for j in state["jobs"].values()):
            state["state"] = "failed"
            state["state_reason"] = "a preserved role result requires explicit retry or correction"
        elif any(q["state"] in PENDING_QUESTIONS and q["blocking"] for q in state["questions"].values()):
            state["state"] = "waiting"
            state["state_reason"] = "specific unanswered questions remain; no answer is simulated"
        elif any(t["state"] == "requested" for t in state["tool_requests"]):
            state["state"] = "waiting"
            state["state_reason"] = "explicit external tool requests await adapter-provided results"
        elif any(t["state"] in {"awaiting_action","retry_requested"} for t in state.get("tool_recoveries", [])):
            state["state"] = "waiting"
            state["state_reason"] = "Python execution requires an explicit recovery decision; see Python tests"
        elif any(e["state"] in {"pending", "deferred"} for e in state["escalations"]):
            state["state"] = "waiting"
            state["state_reason"] = "explicit ancestor escalation or its retained condition remains"
        elif state["topics"]["ROOT"]["state"] in {"delivered","quiescent"}:
            state["state"] = state["topics"]["ROOT"]["state"]
            state["state_reason"] = state["topics"]["ROOT"].get("last_decision", {}).get("reason", "")
        elif any(t.get("state") == "waiting" for t in state["topics"].values()):
            state["state"] = "waiting"
            state["state_reason"] = "scoped dependencies remain; no fresh event requires another model call"
        elif state["state"] == "active":
            state["state"] = "needs_attention"
            state["state_reason"] = "no runnable intent; no parent-authored delivery or quiescence is established"

    def advance(self):
        from .contracts import JobSpec
        with self._tx() as (con, state):
            self._progress(con, state)
            specs = [] if state["paused"] else [j["spec"] for j in self._pending(state)]
        self._export()
        return [JobSpec(**deepcopy(s)) for s in specs]

    def apply(self, result, output=None, receipt=None, *, defer_progress=False):
        """Import a result once; every duplicate/late/invalid raw result survives.

        Queue acknowledgments belong to the caller and occur only after this
        committed receipt. Convenience string form is explicitly a fixture.
        """
        if isinstance(result, str):
            result = {"result_id": "FIXTURE." + digest([result, output]), "job_id": result, "payload": output,
                      "outcome": "succeeded", "attempt_id": "fixture." + result,
                      "receipt": receipt or {"provider": "fixture", "evidence_kind": "fixture"}}
        result = json.loads(encode(result))
        rid = result.get("result_id")
        if not isinstance(rid, str) or not rid:
            raise WorkflowError("result_id is required")
        with self._tx() as (con, state):
            old = con.execute("SELECT body,outcome FROM results WHERE result_id=?", (rid,)).fetchone()
            if old:
                if json.loads(old[0]) != result:
                    raise WorkflowError("result_id conflicts with an immutable original")
                return {"result_id": rid, "status": old[1], "duplicate": True}
            jid = result.get("job_id")
            job = state["jobs"].get(jid)
            if job is None:
                raise WorkflowError("result names no workflow job")
            spec, meta = job["spec"], job["spec"]["metadata"]
            if result.get("run_id", state["run_id"]) != state["run_id"]:
                raise WorkflowError("result belongs to another run")
            # Experience maintenance owns immutable material IDs, not ROOT's
            # business revision. A delivery may advance that head while this
            # independent review finishes. Corrections/epochs still invalidate it.
            stale = (meta["epoch"] != state["epoch"] or
                     (spec["role"] != "memory_review" and meta["revision"] != state["topics"][meta["topic_id"]]["revision"]) or
                     meta.get("topic_epoch", 0) != state["topics"][meta["topic_id"]].get("epoch", 0) or
                     (spec["role"] == "parent" and meta.get("parent_decision_version", 0) != state["topics"][meta["topic_id"]].get("decision_version", 0)))
            if job["state"] == "applied" or stale or job["state"] == "superseded" or result.get("superseded"):
                outcome = "late_preserved" if stale or job["state"] == "superseded" or result.get("superseded") else "duplicate_job_preserved"
                if stale and job["state"] != "applied":
                    job["state"] = "superseded"
                state["late_results"].append({"result_id": rid, "job_id": jid, "reason": outcome,
                                              "state": "private_preserved" if spec["role"] in {"user_review", "memory_review", "processing", "context_summary", "context_review"} else "pending" if outcome == "late_preserved" else "duplicate"})
                if outcome == "late_preserved" and spec["role"] not in {"user_review", "memory_review", "processing", "context_summary", "context_review"}:
                    state["topics"]["ROOT"]["dirty"] = True
                    state["state"] = "active"
            elif result.get("outcome") != "succeeded":
                outcome = "failed"
                job.update(state="failed", error=result.get("error") or "provider failed", result_id=rid)
                job["failure_kind"] = (result.get("error") or {}).get("category", "unknown") if isinstance(result.get("error"),dict) else "unknown"
                job["uncertain"] = result.get("outcome") == "uncertain"
                if job["failure_kind"] == "output_contract" and not job["uncertain"]:
                    job["failure_kind"] = "contract"
                state.update(state="failed", state_reason="a preserved role result requires explicit retry or correction")
            else:
                # Savepoint makes semantic rejection all-or-nothing while still
                # retaining the original for inspection and repair.
                before = StateStore(self.db, con).pack(state)
                con.execute("SAVEPOINT semantic_import")
                try:
                    payload = result["payload"]
                    if spec["role"] in {"parent", "author"}:
                        validate_input_task(json.loads(spec["prompt"])["task"])
                    validate(payload, spec["schema"])
                    if spec["role"] in {"parent", "author"}:
                        validate_progress(payload)
                    job.update(state="applied", result_id=rid, applied_at=now())
                    getattr(self, "_apply_" + spec["role"])(con, state, spec, payload, result)
                    if spec["role"] in {"parent", "author"}:
                        self._archive_work_report(con, state, spec, result)
                        owner = state["topics"][meta["topic_id"]]
                        pressure = owner.pop("context_pressure", None)
                        if pressure:
                            owner.setdefault("capacity_recoveries", []).append({
                                "source_job": pressure["job_id"], "accepted_job": jid,
                                "claim": "Reduced invocation succeeded and was applied; not a measurement of maximum server capacity."})
                    if not defer_progress:
                        self._progress(con, state)
                    outcome = "applied"
                    con.execute("RELEASE semantic_import")
                except (ValueError, KeyError, TypeError, sqlite3.IntegrityError) as exc:
                    con.execute("ROLLBACK TO semantic_import")
                    con.execute("RELEASE semantic_import")
                    state.clear(); state.update(StateStore(self.db, con).decode(before))
                    state["jobs"][jid].update(state="failed", error=str(exc), result_id=rid)
                    state["jobs"][jid]["failure_kind"] = "input_contract" if isinstance(exc, InputContractError) else "contract"
                    state["state"] = "failed"
                    state["state_reason"] = "semantic import rejected: " + str(exc)
                    outcome = "rejected"
            con.execute("INSERT INTO results VALUES(?,?,?)", (rid, encode(result), outcome))
        self._export()
        return {"result_id": rid, "status": outcome, "duplicate": False}

    def _apply_member(self, con, state, spec, output, result):
        meta = spec["metadata"]
        if not output["reason"].strip():
            raise WorkflowError("generation requires a reason, including zero candidates")
        if (output["outcome"] == "proposed") != bool(output["candidates"]):
            raise WorkflowError("generation outcome must agree with actual candidates")
        for c in output["candidates"]:
            if not c["id"].startswith(meta["candidate_prefix"]):
                raise WorkflowError("candidate ID is outside its sealed invocation prefix")
        receipt = result.get("receipt") or {}
        fixture = receipt.get("evidence_kind") == "fixture" or receipt.get("provider") == "fixture"
        execution = {"mode": "fixture"} if fixture else {"mode": "external_import",
                    "provider": str(receipt.get("provider") or "codex_cli"),
                    "invocation_id": result["attempt_id"], "fresh_context_asserted": True}
        self._op(con, "generation", id=self._id(state, "GEN"), packet=meta["packet_id"],
                 candidates=output["candidates"], execution=execution)
        inquiry = state["inquiries"][meta["request_id"]]
        inquiry["candidates"].extend(c["id"] for c in output["candidates"])
        inquiry.setdefault("outcomes", []).append({"job_id":spec["job_id"],"member":meta["member_id"],
            "outcome":output["outcome"],"reason":output["reason"],
            "actual_material_ids":meta.get("evidence_material_ids", []),
            "candidate_ids":[c["id"] for c in output["candidates"]],
            "selection_requests":deepcopy(output["selection_requests"])})
        visible = set(self.parliament._get(con, meta["packet_id"])["material_ids"])
        if state.get("version", 3) >= 4:
            visible |= {m["id"] for m in self._library(con, meta["member_id"])}
        for requested in output["selection_requests"]:
            if not set(requested["material_ids"]) <= visible:
                raise WorkflowError("selection request cites unseen material")
            state["obligations"].append({"id": self._id(state, "SELECT"), "kind": "selection", "state": "open",
                    "topic_id": meta["topic_id"], "member_id": meta["member_id"], "source_job": spec["job_id"], **requested})

    def _files(self, con, spec, files):
        import re

        # Publication runs on Windows too. Reject aliases before committing the
        # semantic result so a database artifact always names one physical file.
        existing = {row[0].casefold() for row in con.execute("SELECT path FROM artifacts WHERE source_job=?", (spec["job_id"],))}
        paths, prepared = [], []
        for file in files:
            raw = file["path"]
            if not isinstance(raw, str) or not raw or len(raw) > 512 or any(ord(c) < 32 or c in '<>:"\\|?*' for c in raw):
                raise WorkflowError("artifact path must be canonical relative POSIX text without Windows reserved characters")
            relative = PurePosixPath(raw)
            if not relative.parts or relative.is_absolute() or raw != relative.as_posix() or any(p in {"..", "."} for p in relative.parts):
                raise WorkflowError("artifact path must be a canonical relative path")
            for part in relative.parts:
                if (part.endswith((".", " ")) or len(part.encode("utf-16-le")) // 2 > 255 or
                        re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|CLOCK\$|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", part.split(".", 1)[0].rstrip(" "))):
                    raise WorkflowError("artifact path contains a Windows reserved filesystem name")
            if len(file["content"].encode("utf-8")) > 4_000_000:
                raise WorkflowError("one artifact exceeds the 4 MB text limit")
            path = "artifacts/" + spec["job_id"] + "/" + relative.as_posix()
            key = path.casefold()
            # Also reserve the publisher's temporary filename. Otherwise a
            # sibling artifact can be overwritten or consumed by tmp.replace.
            occupied = {key, key + ".publishing"}
            for prior in existing:
                previous = {prior, prior + ".publishing"}
                if any(a == b or a.startswith(b + "/") or b.startswith(a + "/") for a in occupied for b in previous):
                    raise WorkflowError("artifact paths collide as Windows files, directories or publication temporary files")
            existing.add(key)
            prepared.append((path, file["content"], spec["job_id"]))
            paths.append(path)
        con.executemany("INSERT INTO artifacts VALUES(?,?,?)", prepared)
        con.executemany('INSERT INTO artifact_headers VALUES(?,?,?,?,?)',
            [(p,j,hashlib.sha256(b.encode('utf-8')).hexdigest(),len(b),len(b.encode('utf-8'))) for p,b,j in prepared])
        return paths

    def _extras(self, con, state, spec, output, result=None):
        topic = spec["metadata"]["topic_id"]
        if sum(t['kind'] == 'attachment.read' for t in output.get('tool_requests', [])) > 4:
            raise WorkflowError('一次最多请求 4 个附件范围；读取返回后再请求后续页或范围')
        self._observe_file_use(state, spec, output, result)
        self._record_input_uses(con, state, spec, output, result)
        self._consume_cognitive_results(state, spec, output)
        self._consume_web(state, spec, output)
        paths = self._files(con, spec, output.get("files", []))
        for path in paths:
            self._model_file(con, state, path, topic)
        for q in output.get("questions", []):
            if state.get("version", 3) >= 4:
                visible_task = json.loads(spec["prompt"])["task"]
                known = state["user_collaboration"]["assets"]
                visible_methods = set()
                def inspect_material(value):
                    if isinstance(value, list):
                        for item in value: inspect_material(item)
                    elif isinstance(value, dict):
                        if value.get("id") in known and value.get("owner") == "USER":
                            try:
                                body = json.loads(value.get("content", ""))
                            except (ValueError, TypeError):
                                body = {}
                            if body.get("content") == known[value["id"]]["content"]:
                                visible_methods.add(value["id"])
                        if "user_collaboration" in value.get("tags", []) and isinstance(value.get("content"), str):
                            try: inspect_material(json.loads(value["content"]))
                            except ValueError: pass
                        for key in ("A", "B"):
                            inspect_material(value.get(key, []))
                inspect_material(visible_task.get("I", []))
                inspect_material(visible_task.get("collaboration_guidance", {}))
                for entry in visible_task.get("user_inputs", []):
                    if entry.get("kind") == "tool_result":
                        inspect_material(entry.get("value", {}).get("payload", {}))
                for gid in q.get("guidance_ids", []):
                    if gid not in known or known[gid]["kind"] != "guidance_method" or gid not in visible_methods:
                        raise WorkflowError("question guidance must name an actually visible method version")
            qid = self._id(state, "QUESTION")
            state["questions"][qid] = {"id": qid, "topic_id": topic, "source_job": spec["job_id"], "state": "open", **q}
            state["questions"][qid]["attachments"] = self._bind_question_files(con, state, spec, q.get("attachments", []))
            if state.get("version", 3) >= 4:
                from . import user_collaboration as user
                question_audience = user.event_audience(state, ["*"], material_refs=q.get("guidance_ids", []))
                state["questions"][qid]["audience"] = question_audience
                user.record_event(state, qid + ".PROPOSED", "question_proposed", source="model", topic_id=topic,
                    question_id=qid, job_id=spec["job_id"], revision_id=spec["metadata"]["revision"], content=encode(q),
                    allowed_uses=["profile", "collaboration"], audience=question_audience, material_refs=q.get("guidance_ids", []))
            self._stage_question(state, spec, state["questions"][qid])
        for o in output.get("unresolved", []):
            prior = next((x for x in state["obligations"] if x["topic_id"] == topic and x["state"] == "conditional"
                          and all(x.get(k) == o.get(k) for k in ("text", "condition", "next_action"))), None)
            if prior:
                prior["last_reaffirmed_by"] = spec["job_id"]
                prior["reaffirmations"] = prior.get("reaffirmations",0) + 1
            else:
                state["obligations"].append({"id": self._id(state, "OBLIGATION"), "topic_id": topic,
                                             "state": "conditional", "source_job": spec["job_id"], **o})
        for tool in output.get("tool_requests", []):
            try:
                arguments = json.loads(tool["arguments"])
            except ValueError as exc:
                raise WorkflowError("tool request arguments must be JSON") from exc
            if not isinstance(arguments, dict):
                raise WorkflowError("tool request arguments must be a JSON object")
            if isinstance(arguments, dict) and isinstance(arguments.get("path"), str) and arguments["path"].startswith("@self/"):
                arguments["path"] = "artifacts/" + spec["job_id"] + "/" + arguments["path"][6:]
            if tool["kind"] == "sandbox.python_tests" and isinstance(arguments.get("files"), list):
                for item in arguments["files"]:
                    if isinstance(item, dict) and isinstance(item.get("path"), str) and item["path"].startswith("@self/"):
                        item["path"] = "artifacts/" + spec["job_id"] + "/" + item["path"][6:]
            state["tool_requests"].append({"id": self._id(state, "TOOL"), "topic_id": topic, "state": "requested",
                                            "source_job": spec["job_id"], **tool, "arguments": arguments})
        self._internal_actions(con, state, spec, output)
        self._record_work(state, spec, output)
        return paths

    def _returned(self, con, state, spec, output, *, partial=False):
        meta = spec["metadata"]
        revision = self.parliament._get(con, meta["revision"], "revision")
        attributed = {c for item in output["contributions"] for c in item["candidate_ids"]}
        if not set(revision["candidates"]) <= attributed:
            raise WorkflowError("every assigned candidate requires an explicit contribution")
        signature = digest([meta["revision"], output["ans"], output["con"], output["contributions"], partial])
        returns = state.setdefault("return_signatures", {})
        if signature in returns:
            state.setdefault("duplicate_return_receipts", []).append({"source_job":spec["job_id"],"return_id":returns[signature]})
            return returns[signature]
        ret = self._id(state, "RETURN")
        returns[signature] = ret
        self._op(con, "return", id=ret, revision=meta["revision"], ans=output["ans"],
                 con_text=output["con"], contributions=output["contributions"])
        topic = state["topics"][meta["topic_id"]]
        topic.setdefault("awaiting_feedback", []).append(ret)
        state["return_sources"][ret] = spec["job_id"]
        state.setdefault("return_kinds", {})[ret] = "checkpoint" if partial else "complete"
        if state.get("version", 3) >= 4:
            self._schedule_intent(state, "assessments", meta["topic_id"], source_job=spec["job_id"], returned=ret,
                                  contributions=deepcopy(output["contributions"]))
        else:
            self._schedule_assessments(con, state, spec, ret, output)
        return ret

    def _schedule_assessments(self, con, state, spec, ret, output):
        if state.get('version', 3) >= 4:
            return self._schedule_assessment_groups(con, state, spec, ret, output)
        return self._schedule_single_assessments(con, state, spec, ret, output)

    def _schedule_assessment_groups(self, con, state, spec, ret, output):
        """One fresh call per same-owner group, with separate decisions.

        Ownership and invitations remain per contribution. Oversized groups are
        split by the actual input bound, never by a fixed exploration count.
        No cross-member memory is shared and no judgment is implicitly copied.
        """
        from .roles import ASSESS_SCHEMA, obj, arr
        meta, grouped = spec['metadata'], {}
        for index, contribution in enumerate(output['contributions']):
            candidates = [self.parliament._get(con, c, 'candidate') for c in contribution['candidate_ids']]
            owners = sorted({c['member'] for c in candidates}) or [state['members'][0]['id']]
            for owner in owners:
                invitation = None
                if not candidates:
                    invitation = 'INVITE.' + digest([ret,index,owner])[:24]
                    if not con.execute('SELECT 1 FROM objects WHERE id=?', (invitation,)).fetchone():
                        self._op(con, 'review_invitation', id=invitation, member=owner, returned=ret, contribution_index=index,
                            scope='unattributed author discovery', purpose='fresh assessment without assuming candidate ownership',
                            source={'kind':'observation','reference':spec['job_id']})
                grouped.setdefault(owner, []).append({'contribution_index':index,'contribution':contribution,
                    'candidates':[c for c in candidates if c['member'] == owner],'invitation':invitation})
        created = []
        for owner, entries in grouped.items():
            member = next(m for m in state['members'] if m['id'] == owner)
            done = {e['contribution_index'] for j in state['jobs'].values()
                if j['spec']['role'] == 'assessment' and j['spec']['metadata'].get('returned') == ret
                and j['spec']['metadata'].get('member_id') == owner and j['state'] != 'superseded'
                and j['spec']['metadata'].get('assessment_cycle') == meta.get('assessment_cycle')
                for e in j['spec']['metadata'].get('assessment_entries', [])}
            entries = [e for e in entries if e['contribution_index'] not in done]
            if not entries: continue
            sources = self._sources(con, state, references=[ret], member=owner)
            required, manifest = self._source_materials(con, state, owner, sources, topic=meta['topic_id'], query=encode(entries))
            library = self._selection_library(con, state, owner, topic=meta['topic_id'], query=encode(entries))
            limits = self.parliament._get(con, 'CONFIG', 'config')['limits']
            groups, prepared = [entries], False
            while groups:
                batch = groups.pop(0)
                entry_schema = obj(contribution_index={'type':'integer','enum':[e['contribution_index'] for e in batch]}, **deepcopy(ASSESS_SCHEMA['properties']))
                schema = obj(assessments=arr(entry_schema)) if len(batch)>1 else deepcopy(ASSESS_SCHEMA)
                task = {'returned':ret,'revision':meta['revision'], 'assessment_entries':batch,
                    'author_source':{'job_id':spec['job_id'],'result_id':state['jobs'][spec['job_id']]['result_id']},
                    'goal_source':{'kind':'provided_task','text':state['goal']},
                    'boundary':'All entries belong to this member and same author return. Judge each separately, including uncertain/insufficient. One response is not a shared score. Prepared sources disclose original IDs and coverage; missing evidence is not evidence of failure.'}
                if len(batch)==1:
                    task.update(contribution_index=batch[0]['contribution_index'],contribution=batch[0]['contribution'],candidates=batch[0]['candidates'])
                try:
                    prompt, selection = select_input('assessment', member, task, library, required_ids=required, limits=limits, output_schema=schema)
                except SelectionBudgetError:
                    if not prepared:
                        required, library = self._prepare_council_sources(con, state, meta['topic_id'], member, required, library, limits)
                        prepared = True; groups.insert(0,batch); continue
                    if len(batch)==1: raise
                    cut = len(batch)//2; groups[:0] = [batch[:cut],batch[cut:]]; continue
                jid = self._job(state, 'assessment', meta['topic_id'], prompt,
                    {'returned':ret,'member_id':owner,'assessment_entries':batch,'batched_assessment':len(batch)>1,
                     'assessment_cycle':meta.get('assessment_cycle'),
                     'contribution_index':batch[0]['contribution_index'] if len(batch)==1 else None,
                     'invitation':batch[0]['invitation'] if len(batch)==1 else None,
                     'input_accounting':selection['accounting'],'evidence_material_ids':required}, schema=schema)
                created.append(jid)
                state['selections'].append({'returned':ret,'member':owner,'selection':selection})
                state['evidence_deliveries'].append({'returned':ret,'contribution_indices':[e['contribution_index'] for e in batch],
                    'member_id':owner,'source_manifest':manifest,'batches':[{'job_id':jid,'material_ids':required}],
                    'state':'sealed_for_dispatch','not_claimed':'model consumption or semantic adequacy'})
        return created

    def _schedule_single_assessments(self, con, state, spec, ret, output):
        meta = spec["metadata"]
        created = []
        for index, contribution in enumerate(output["contributions"]):
            candidates = [self.parliament._get(con, c, "candidate") for c in contribution["candidate_ids"]]
            owners = sorted({c["member"] for c in candidates})
            if not owners:
                owners = [state["members"][0]["id"]]
            for owner in owners:
                invitation = None
                if not candidates:
                    invitation = self._id(state, "INVITE")
                    self._op(con, "review_invitation", id=invitation, member=owner, returned=ret, contribution_index=index,
                             scope="unattributed author discovery", purpose="fresh assessment without assuming candidate ownership",
                             source={"kind": "observation", "reference": spec["job_id"]})
                member = next(m for m in state["members"] if m["id"] == owner)
                sources = self._sources(con, state, references=[ret], member=owner)
                required, manifest = self._source_materials(con, state, owner, sources,
                    topic=meta["topic_id"], query=contribution["text"])
                task = {"returned": ret, "revision": meta["revision"], "contribution_index": index,
                        "contribution": contribution, "candidates": [c for c in candidates if c["member"] == owner],
                        "author_source": {"job_id": spec["job_id"], "result_id": state["jobs"][spec["job_id"]]["result_id"]},
                        "goal_source": {"kind": "provided_task", "text": state["goal"]},
                        "evidence_scope": {"batch": 9999, "batches": 9999, "source_ids": [s["id"] for s in sources],
                                           "rule": "Judge only actual source ranges in this batch; other ranges are assessed separately. Missing excerpts are not missing artifacts. Supplied engineering facts are not executed verification."}}
                library = self._selection_library(con, state, owner, topic=meta["topic_id"], query=contribution["text"])
                limits = self.parliament._get(con, "CONFIG", "config")["limits"]
                batches = evidence_batches("assessment", member, task, library, required, limits=limits)
                delivered = []
                for number, batch in enumerate(batches):
                    task["evidence_scope"].update(batch=number + 1, batches=len(batches))
                    prompt, selection = select_input("assessment", member, task, library, required_ids=batch, limits=limits)
                    state["selections"].append({"returned": ret, "member": owner, "selection": selection})
                    jid = self._job(state, "assessment", meta["topic_id"], prompt,
                              {"returned": ret, "contribution_index": index, "member_id": owner,
                               "invitation": invitation, "input_accounting": selection["accounting"],
                               "evidence_material_ids": batch, "evidence_batch": number + 1, "evidence_batches": len(batches)})
                    created.append(jid)
                    delivered.append({"job_id": jid, "material_ids": batch})
                state["evidence_deliveries"].append({"returned": ret, "contribution_index": index, "member_id": owner,
                    "source_manifest": manifest, "batches": delivered, "state": "sealed_for_dispatch",
                    "not_claimed": "model consumption or semantic adequacy"})
        return created

    def _apply_author(self, con, state, spec, output, result):
        topic = state["topics"][spec["metadata"]["topic_id"]]
        notice = output.get("parent_notice", {})
        if notice.get("priority") == "urgent":
            if not notice.get("reason", "").strip():
                raise WorkflowError("urgent parent notice requires a concrete reason")
            parent = state["topics"][topic["parent"]]
            parent.setdefault("feedback_notices", []).append({"source_job":spec["job_id"],
                "topic_id":topic["id"], "revision":topic["revision"], "reason":notice["reason"]})
            parent["dirty"] = True
        self._extras(con, state, spec, output, result)
        topic["summary"] = output["ans"]
        if output["status"] == "checkpoint":
            if not output["ans"].strip() or not output["con"].strip():
                raise WorkflowError("checkpoint requires a substantive partial result and its boundary")
            self._apply_wait(state, spec, output)
            if not output["contributions"] and output.get("progress", {}).get("kind") == "operational":
                topic["checkpoint"] = {"source_job":spec["job_id"], "operational":True,
                    "remaining_dependencies":deepcopy(topic.get("wait_dependencies", []))}
                return
            if not output["contributions"]:
                raise WorkflowError("cognitive checkpoint requires contributions; operational progress must not invent them")
            ret = self._returned(con, state, spec, output, partial=True)
            topic["checkpoint"] = {"return_id":ret, "source_job":spec["job_id"],
                "remaining_dependencies":deepcopy(topic.get("wait_dependencies", []))}
        elif output["status"] == "complete":
            if self._internal_waits(state, spec):
                raise WorkflowError("author cannot complete while an internal dependency remains")
            if any(d["topic_id"] == topic["id"] and d["state"] == "requested" for d in state["tool_requests"]):
                raise WorkflowError("author cannot complete while tool execution is pending")
            if any(d["topic_id"] == topic["id"] and d["state"] in {"awaiting_action","retry_requested"}
                   for d in state.get("tool_recoveries", [])):
                raise WorkflowError("author cannot complete while test execution recovery is pending")
            if any(q["topic_id"] == topic["id"] and q["state"] in PENDING_QUESTIONS
                   and (q["blocking"] or q["state"] == "proposed") for q in state["questions"].values()):
                raise WorkflowError("author cannot complete while its user task awaits coordination or an answer")
            if any(h["recipient"] == topic["id"] and h["state"] not in {"delivered","cancelled"}
                   for h in state.get("question_handoffs", [])):
                raise WorkflowError("author cannot complete while a required handoff is pending")
            if any(state["input_receipts"].get(i["id"], {}).get("state") != "processed"
                   for i in state["user_inputs"] if i.get("topic_id") in {None, topic["id"]}):
                raise WorkflowError("author cannot complete while submitted inputs remain unprocessed")
            self._returned(con, state, spec, output)
            topic["state"] = "assessing"
        elif output["status"] == "consult":
            if state.get("version", 3) >= 4:
                self._schedule_intent(state, "inquiry", topic["id"], question=output["inquiry"])
                topic["state"] = "consulting"
            else:
                self._inquiry(con, state, topic["id"], output["inquiry"])
        else:
            if state.get("version", 3) >= 4:
                self._apply_wait(state, spec, output)
            else:
                if not output["questions"] and not output["tool_requests"]:
                    raise WorkflowError("waiting author must name a question or external tool dependency")
                topic["state"] = "waiting"

    def _apply_wait(self, state, spec, output):
        """Honor real sealed dependencies even if they resolved during the call.

        The original output remains a wait request; only its effective execution
        state changes. References in summaries or public history prove nothing.
        """
        topic_id = spec["metadata"]["topic_id"]
        topic = state["topics"][topic_id]
        task = json.loads(spec["prompt"])["task"]
        pending, resolved = self._internal_waits(state, spec), []
        # A delivered response may overtake the call that was waiting for it.
        # Accept only the sealed dependency identity and its real targeted
        # input, never a bare mention of an old resolved request in history.
        for sealed in task.get('dependencies', {}).get('outgoing', []):
            actual = next((r for r in state.get('internal_requests', []) if r['id'] == sealed['id']), None)
            if (actual and actual['requester'] == topic_id and actual['source_job'] == sealed['source_job']
                    and sealed['state'] in {'open','deferred'} and actual['state'] in {'delivered','consumed'}
                    and any(i['id'] == actual.get('input_id') and i.get('internal_request_id') == actual['id']
                            and i.get('topic_id') == topic_id for i in state['user_inputs'])):
                resolved.append({'kind':'internal','id':actual['id'],'state':actual['state'],'input_id':actual['input_id']})
        tables = [("tool", "tool_requests", "id", state["tool_requests"], {"requested"}),
                  ("tool_recovery", "tool_recoveries", "id", state.get("tool_recoveries", []), {"awaiting_action","retry_requested"}),
                  ("question", "questions", "id", list(state["questions"].values()), PENDING_QUESTIONS),
                  ("escalation", "escalations", "disposition_id", state["escalations"], {"pending", "deferred"})]
        for kind, field, key, current, open_states in tables:
            by_id = {item[key]: item for item in current}
            for sealed in task.get(field, []):
                actual = by_id.get(sealed.get(key))
                if (sealed.get("state") not in open_states or actual is None
                        or actual.get("receiver" if kind == "escalation" else "topic_id") not in (
                            self._scope(state,topic_id) if kind == "tool_recovery" and spec["role"] == "parent" else {topic_id})
                        or sealed.get("source_job") != actual.get("source_job")):
                    continue
                entry = {"kind": kind, "id": actual[key], "state": actual["state"]}
                if actual["state"] in open_states:
                    pending.append(entry)
                elif kind == "tool" and actual["state"] == "answered" and any(
                        item.get("id") == actual.get("result_id") and item.get("request_id") == actual[key] for item in state["user_inputs"]):
                    resolved.append({**entry, "input_id": actual["result_id"]})
                elif kind == "question" and actual["state"] == "answered" and any(
                        item.get("id") == actual.get("answer_id") and item.get("question_id") == actual[key] for item in state["user_inputs"]):
                    resolved.append({**entry, "input_id": actual["answer_id"]})
                elif kind == "escalation" and actual["state"] in {"handled", "rejected"}:
                    resolved.append(entry)
                elif kind == "tool_recovery" and actual["state"] in {"resolved","dismissed"}:
                    resolved.append(entry)
                elif kind == "question" and actual["state"] in {"resolved", "cancelled"}:
                    resolved.append(entry)
            # Dependencies created by this response also authorize waiting.
            pending.extend({"kind": kind, "id": item[key], "state": item["state"]} for item in current
                           if item.get("source_job") == spec["job_id"] and item["state"] in open_states)
        if spec["role"] == "parent":
            # Retain the existing parent's ability to wait on actual subtree
            # duties; the new arrival-race path itself requires sealed proof.
            scope = self._scope(state, topic_id)
            for kind, _, key, records, open_states in tables:
                pending.extend({"kind": kind, "id": item[key], "state": item["state"]} for item in records
                               if item["state"] in open_states and (item.get("topic_id") in scope or item.get("receiver") in scope))
        dependencies = task.get("interaction", {}).get("dependencies", [])
        for sealed in dependencies:
            actual = next((h for h in state.get("question_handoffs", []) if h["id"] == sealed["id"]
                           and h["recipient"] == topic_id), None)
            if (actual and sealed.get("state") in {"awaiting_answer","awaiting_report"}
                    and sealed.get("source_job") == actual.get("source_job")):
                entry = {"kind":"handoff","id":actual["id"],"state":actual["state"]}
                if actual["state"] in {"awaiting_answer","awaiting_report"}:
                    pending.append(entry)
                elif actual["state"] == "cancelled":
                    resolved.append(entry)
                elif actual["state"] == "delivered" and any(
                        i["id"] == actual.get("input_id") and i.get("topic_id") == topic_id
                        for i in state["user_inputs"]):
                    resolved.append({**entry,"input_id":actual["input_id"]})
        # Include genuinely current dependencies as wait bases, never as new
        # arrivals. A parent may wait on subtree work, including a partial
        # delivery's still-open handoff. Old delivered records are inert.
        scope = self._scope(state, topic_id) if spec["role"] == "parent" else {topic_id}
        pending.extend({"kind":"handoff","id":h["id"],"state":h["state"]}
                       for h in state.get("question_handoffs", [])
                       if h["state"] in {"awaiting_answer","awaiting_report"}
                       and (h["recipient"] == topic_id or spec["role"] == "parent" and h["coordinator"] in scope))
        if spec["role"] == "parent":
            pending.extend({"kind":"author_refresh","id":tid,"state":"requested"}
                           for tid in scope - {topic_id}
                           if state["topics"][tid].get("author_refresh_required"))
            pending.extend({"kind":"failed_call","id":j["spec"]["job_id"],"state":"failed"}
                           for j in state["jobs"].values() if j["state"] == "failed"
                           and j["spec"]["metadata"]["topic_id"] in scope - {topic_id}
                           and j["spec"]["role"] not in {"memory_review","user_review"})
            pending.extend({"kind":"job","id":j["spec"]["job_id"],"state":j["state"]}
                           for j in self._pending(state) if j["spec"]["metadata"]["topic_id"] in scope
                           and j["spec"]["role"] not in {"memory_review","user_review"})
            # The same race exists for subtree calls: a parent may have been
            # asked to wait while a child/assessment was running. Accept that
            # historical wait, then expose the actual newly returned work once.
            for sealed in task.get("active_jobs", []):
                job = state["jobs"].get(sealed.get("id"), {})
                actual_spec = job.get("spec", {})
                if (actual_spec.get("metadata", {}).get("topic_id") in scope
                        and actual_spec.get("metadata", {}).get("topic_id") == sealed.get("topic_id")
                        and actual_spec.get("role") == sealed.get("role")
                        and actual_spec.get("role") not in {"memory_review","user_review"}
                        and job.get("state") in {"applied", "failed", "superseded"}):
                    resolved.append({"kind":"job","id":sealed["id"],"state":job["state"]})
            pending.extend({"kind":"schedule","id":i["id"],"state":i["state"]}
                           for i in state["schedule_intents"] if i["topic_id"] in scope and i["state"] == "pending")
        # Deduplicate administrative references without merging different events.
        pending = list({(p["kind"],p["id"]):p for p in pending}.values())
        resolved = list({(p["kind"],p["id"]):p for p in resolved}.values())
        if not pending and not resolved:
            raise WorkflowError("wait requires an actual question or external dependency")
        if resolved:
            # A child finishing one call may simply have started another tool
            # wait. Its checkpoint/final return has its own assessed delivery
            # trigger. Do not wake the parent merely to observe that substep
            # while other real dependencies still support the requested wait.
            wakeup_required = not pending or any(item["kind"] != "job" for item in resolved)
            state["diagnostics"].append({"kind": "wait_dependency_resolved_before_apply", "job_id": spec["job_id"],
                "requested_action": "wait", "effective_state": "waiting" if pending else "active",
                "wakeup_required":wakeup_required,
                "resolved_dependencies": resolved, "remaining_dependency_ids": sorted({item["id"] for item in pending}),
                "boundary": "Wait was valid in the sealed input. Resolution is transport state, not model reading or semantic verification."})
            if wakeup_required and spec["role"] == "author":
                topic["author_refresh_required"] = True
            elif wakeup_required:
                topic["dirty"] = True
        topic["state"] = "waiting" if pending else "active"
        topic["wait_dependencies"] = pending
        topic["wait_source_job"] = spec["job_id"]
        if spec["role"] == "parent":
            state["state"] = topic["state"]
            state["state_reason"] = output.get("reason", "") if pending else "sealed wait dependencies resolved before application; fresh input is ready"

    def _apply_assessment(self, con, state, spec, output, result):
        meta = spec["metadata"]
        if meta.get('batched_assessment'):
            entries = {e['contribution_index']:e for e in meta['assessment_entries']}
            if sorted(o['contribution_index'] for o in output['assessments']) != sorted(entries):
                raise WorkflowError('assessment group must judge each sealed contribution exactly once')
            for item in output['assessments']:
                entry = entries[item['contribution_index']]
                single = {**spec,'metadata':{**meta,**entry,'batched_assessment':False}}
                self._apply_assessment(con,state,single,{k:v for k,v in item.items() if k != 'contribution_index'},result)
            return
        aid = self._id(state, "ASSESS")
        self._op(con, "assessment", id=aid, member=meta["member_id"], returned=meta["returned"],
                 contribution_index=meta["contribution_index"], judgment=output["judgment"], reason=output["reason"],
                 invitation=meta.get("invitation"))
        state["assessment_sources"][aid] = spec["job_id"]
        self._op(con, "learn", id=self._id(state, "LEARN"), assessment=aid,
                 abstract=output["abstract"] + "\n条件/未知：" + output["conditions"],
                 scenario=output["scenario"] + "\n条件/未知：" + output["conditions"],
                 source={"kind": _result_source_kind(result),
                         "reference": "result:" + result["result_id"] + "; job:" + spec["job_id"]}, promote=output["promote"])

    def _apply_parent(self, con, state, spec, output, result):
        meta = spec["metadata"]
        topic = state["topics"][meta["topic_id"]]
        inquiry_uses = output.get("inquiry_uses", [])
        if sorted(u["request_id"] for u in inquiry_uses) != sorted(meta.get("inquiry_result_ids", [])):
            raise WorkflowError("parent must acknowledge each delivered generation outcome, including empty candidates")
        for use in inquiry_uses:
            if not use["use"].strip() or not use["effect"].strip():
                raise WorkflowError("inquiry outcome needs explicit use and effect; no candidates is not proof of completed work")
            state["inquiries"][use["request_id"]].setdefault("uptake", []).append({"job_id":spec["job_id"],**use})
        projection = meta.get("context_projection", {})
        if (projection.get("batch_count", 1) > 1 or not projection.get("required_scope_complete", True)) and output["action"] in {"deliver", "quiesce"}:
            raise WorkflowError("remaining required context groups must be processed before a terminal decision")
        decisions = output["dispositions"]
        if sorted(d["candidate_id"] for d in decisions) != sorted(meta["candidate_ids"]):
            raise WorkflowError("parent must dispose every supplied candidate exactly once")
        if sorted(d["return_id"] for d in output["contribution_decisions"]) != sorted(meta["return_ids"]):
            raise WorkflowError("parent must decide every newly returned contribution group exactly once")
        if sorted(d["request_id"] for d in output.get("selection_responses", [])) != sorted(meta.get("selection_ids", [])):
            raise WorkflowError("parent must respond to every supplied selection request exactly once")
        for response in output.get("selection_responses", []):
            obligation = next(o for o in state["obligations"] if o["id"] == response["request_id"])
            if not response["reason"].strip():
                raise WorkflowError("selection response requires an explicit reason")
            if response["action"] == "reselect" and output["action"] != "consult":
                raise WorkflowError("reselect requires an actual new consult input")
            if response["action"] == "ask" and not output["questions"]:
                raise WorkflowError("selection ask requires an actual question")
            obligation.update(state="conditional" if response["action"] == "defer" else "responded",
                              response=response, response_job=spec["job_id"])
        if sorted(e["disposition_id"] for e in output.get("escalation_responses", [])) != sorted(meta.get("escalation_ids", [])):
            raise WorkflowError("parent must respond to every supplied internal escalation exactly once")
        for response in output.get("escalation_responses", []):
            escalation = next(e for e in state["escalations"] if e["disposition_id"] == response["disposition_id"])
            self._op(con, "resolve", id=self._id(state, "RESOLVE"), disposition=response["disposition_id"],
                     receiver=topic["id"], outcome=response["outcome"], note=response["note"])
            escalation.update(state=response["outcome"], response=response, response_job=spec["job_id"])
            if response["outcome"] == "handled":
                state["topics"][escalation["topic_id"]]["dirty"] = True
        for late in state["late_results"]:
            if late["result_id"] in meta.get("late_ids", []):
                late.update(state="reviewed", review_job=spec["job_id"])
        spec = {**spec, "_publication_snapshot": interaction_fingerprint(state, topic["id"])}
        self._interaction_decisions(con, state, spec, output)
        files = self._extras(con, state, spec, output, result)
        for path in output.get("stage_artifacts", []):
            if path.startswith("@self/"): path = "artifacts/" + spec["job_id"] + "/" + path[6:]
            row = con.execute("SELECT source_job FROM artifacts WHERE path=?", (path,)).fetchone()
            if not row or state["jobs"][row[0]]["spec"]["metadata"]["topic_id"] not in self._scope(state, topic["id"]):
                raise WorkflowError("stage delivery must bind an actual scoped artifact version")
            if path not in files: files.append(path)
        adopted = []
        for d in decisions:
            c = self.parliament._get(con, d["candidate_id"], "candidate")
            request = self.parliament._get(con, c["request"], "request")
            did = self._id(state, "DISPOSITION")
            ancestors = self.parliament._ancestors(con, topic["id"])
            local_effect = None
            if d["action"] == "adopt_local":
                if not all(v.strip() for v in (d["reason"],d["relation"],output["summary"])):
                    raise WorkflowError("local adoption requires an actual summary, relation and concrete effect")
                local_effect = encode({"summary":output["summary"], "effect":d["reason"], "relation":d["relation"],
                    "artifact_paths":files, "source_job":spec["job_id"], "source_result":result["result_id"],
                    "boundary":"Parent's actual local processing, not an independently assessed child contribution."})
            self._op(con, "disposition", id=did, request=c["request"], candidate=c["id"],
                     action="adopt" if d["action"] == "adopt_local" else d["action"], local_effect=local_effect,
                     reason=d["reason"], replacement=d["replacement"] or None,
                     target=ancestors[0] if d["action"] == "escalate" and ancestors else None)
            if d["action"] == "adopt":
                adopted.append((request, did, d["relation"]))
            elif d["action"] == "escalate":
                if ancestors:
                    state["escalations"].append({"disposition_id": did, "topic_id": topic["id"], "receiver": ancestors[0],
                        "candidate_id": c["id"], "reason": d["reason"], "state": "pending", "source_job": spec["job_id"]})
                    state["topics"][ancestors[0]]["dirty"] = True
                else:
                    qid = self._id(state, "QUESTION")
                    state["questions"][qid] = {"id": qid, "topic_id": topic["id"], "source_job": spec["job_id"],
                         "state": "open", "text": c["q"], "why": d["reason"], "blocking": True, "disposition": did}
                    self._stage_question(state, spec, state["questions"][qid])
                    if state.get("version", 3) >= 4:
                        from . import user_collaboration as user
                        user.record_event(state, qid + ".PROPOSED", "question_proposed", source="model", topic_id=topic["id"],
                            question_id=qid, content=encode(state["questions"][qid]), job_id=spec["job_id"],
                            allowed_uses=["profile", "collaboration"], audience=["*"])
        groups = {}
        for request, did, relation in adopted:
            key = request["child"] or self._id(state, "TOPIC")
            groups.setdefault(key, {"request": request, "dispositions": [], "relations": []})
            groups[key]["dispositions"].append(did)
            groups[key]["relations"].append(relation)
        for child, group in groups.items():
            rev = self._id(state, "REV")
            self._op(con, "revision", id=rev, topic=child, request=group["request"]["id"], dispositions=group["dispositions"],
                     relation="; ".join(group["relations"]) or "parent-adopted contribution", continuity="fresh",
                     unresolved_previous="Previous unresolved obligations retained in workflow and explicit parent input.")
            prior = state["topics"].get(child, {})
            state["topics"][child] = {**prior, "id": child, "parent": topic["id"], "revision": rev,
                                       "state": "active", "summary": prior.get("summary", ""),
                                       "new_returns": [], "consumed_returns": prior.get("consumed_returns", []), "dirty": False}
            if state.get("version", 3) >= 4:
                self._schedule_intent(state, "author", child)
            else:
                self._author(con, state, child)
        from .feedback import open_wave
        open_wave(state, topic["id"], spec["job_id"], list(groups))
        candidate_children = {}
        for child, group in groups.items():
            for did in group["dispositions"]:
                disposition = self.parliament._get(con, did, "disposition")
                candidate_children[disposition["candidate"]] = child
        self._question_handoff_actions(con, state, spec, output, candidate_children)
        for notice in topic.get("feedback_notices", []):
            if notice["source_job"] in meta.get("feedback_notice_ids", []):
                notice["handled"] = spec["job_id"]
        for rid in meta["request_ids"]:
            inquiry = state["inquiries"][rid]
            inquiry.setdefault("disposed", []).extend(c for c in meta["candidate_ids"] if c in inquiry["candidates"])
            if set(inquiry["candidates"]) <= set(inquiry["disposed"]) and (state.get("version",3) < 4 or inquiry.get("uptake")):
                self._op(con, "close_request", id=self._id(state, "CLOSE"), request=rid,
                         reason="all fresh generation results and explicit parent dispositions imported")
                inquiry["state"] = "closed"
            else:
                inquiry["state"] = "ready"
                topic["dirty"] = True
        for ret in meta["return_ids"]:
            topic["new_returns"].remove(ret)
            topic["consumed_returns"].append(ret)
        if topic["new_returns"] or meta.get("context_projection", {}).get("batch_count", 1) > 1:
            topic["dirty"] = True
        topic["summary"] = output["summary"]
        topic["decision_version"] = topic.get("decision_version", 0) + 1
        topic["last_decision"] = {"job_id": spec["job_id"], "action": output["action"], "reason": output["reason"],
                                  "contribution_decisions": output["contribution_decisions"], "source_result": result["result_id"]}
        if state.get("version", 3) >= 4:
            topic["last_decision"]["summary_source"] = {"sha256": digest(output["summary"]),
                                                       "report_source_kind": _result_source_kind(result)}
        action = output["action"]
        if action == "consult":
            child = output["child_topic"] or None
            if child and (child not in state["topics"] or state["topics"][child]["parent"] != topic["id"]):
                raise WorkflowError("consult child must be a direct owned topic")
            responses = [r for r in output.get("selection_responses", []) if r["action"] == "reselect"]
            requests = [o for o in state["obligations"] if o["id"] in {r["request_id"] for r in responses}]
            if state.get("version", 3) >= 4:
                self._schedule_intent(state, "inquiry", topic["id"], question=output["inquiry"].replace("@self/", "artifacts/" + spec["job_id"] + "/"), child=child,
                    source_refs=meta.get("return_ids", []), source_text=encode(responses + requests).replace("@self/", "artifacts/" + spec["job_id"] + "/"),
                    responding_to=[r["request_id"] for r in responses])
                topic["state"] = "consulting"
            else:
                self._inquiry(con, state, topic["id"], output["inquiry"], child,
                              source_refs=meta.get("return_ids", []), source_text=encode(responses + requests).replace("@self/", "artifacts/" + spec["job_id"] + "/"),
                              responding_to=[r["request_id"] for r in responses])
            for request in requests:
                request["state"] = "delivery_pending" if state.get("version", 3) >= 4 else "delivery_prepared"
                request["delivery_receipts"] = [d for d in state["evidence_deliveries"] if request["id"] in d.get("responding_to", [])]
                request["delivery_claim"] = "Follow-up delivery is queued; no new source range is claimed as delivered yet." if state.get("version", 3) >= 4 else "Actual material IDs and complete source ranges are sealed; consumption and adequacy are not inferred."
        elif action in {"deliver", "quiesce"}:
            if self._internal_waits(state, spec):
                raise WorkflowError("terminal decision cannot bypass internal dependencies")
            if state.get("version", 3) >= 4:
                inputs = [item for item in state["user_inputs"] if item.get("topic_id") in {None, topic["id"]}]
                if any(state["input_receipts"].get(item["id"], {}).get("state") != "processed" for item in inputs):
                    raise WorkflowError("terminal decision cannot bypass unprocessed inputs, including arrivals after this call was sealed")
            scope = self._scope(state, topic["id"])
            blocking = [q for q in state["questions"].values() if q["topic_id"] in scope
                        and (q["state"] == "proposed" or q["state"] in PENDING_QUESTIONS and q["blocking"])]
            if any(h["coordinator"] in scope and h["state"] not in {"delivered","cancelled"} for h in state.get("question_handoffs", [])):
                raise WorkflowError("terminal decision cannot bypass pending question handoffs")
            selections = [o for o in state["obligations"] if o["topic_id"] in scope and o.get("kind") == "selection" and o["state"] == "open"]
            pending = [j for j in self._pending(state) if j["spec"]["metadata"]["topic_id"] in scope
                       and j["spec"]["role"] not in {"memory_review","user_review"}]
            pending_schedules = [i for i in state["schedule_intents"] if i["state"] == "pending" and i["topic_id"] in scope]
            if any(state["topics"][tid].get("author_refresh_required") or state["topics"][tid].get("dirty")
                   for tid in scope - {topic["id"]} if not state["topics"][tid].get("fixed")):
                raise WorkflowError("terminal decision cannot bypass registered descendant work awaiting dispatch")
            if any(state["input_receipts"].get(i["id"], {}).get("state") != "processed"
                   for i in state["user_inputs"] if i.get("topic_id") in scope - {topic["id"]}
                   and not state["topics"][i["topic_id"]].get("fixed")):
                raise WorkflowError("terminal decision cannot bypass a descendant's unprocessed scoped input")
            if any(e["state"] == "pending" and (e["topic_id"] in scope or e["receiver"] in scope) for e in state["escalations"]):
                raise WorkflowError("terminal decision cannot bypass unresolved internal escalation")
            if pending or pending_schedules or blocking or selections or any(t["new_returns"] for t in state["topics"].values() if t["id"] in scope):
                raise WorkflowError("terminal decision cannot bypass pending calls, questions, returns or selection requests")
            if any(x["state"] == "requested" for x in state["tool_requests"] if x["topic_id"] in scope):
                raise WorkflowError("external tool dependencies remain")
            if any(x["state"] in {"awaiting_action","retry_requested"} for x in state.get("tool_recoveries", [])
                   if x["topic_id"] in scope):
                raise WorkflowError("test execution recovery remains")
            if not output["reason"].strip():
                raise WorkflowError("delivery/quiescence requires an explicit parent-authored basis")
            if topic["id"] == "ROOT":
                if action == "deliver" and not con.execute("SELECT 1 FROM artifacts LIMIT 1").fetchone():
                    raise WorkflowError("delivery requires a checkable artifact")
                returned = self._id(state, "RETURN")
                self._op(con, "return", id=returned, revision=topic["revision"], ans=output["summary"],
                         con_text=output["reason"], contributions=[])
                new_rev = self._id(state, "ROOT.REV")
                self._op(con, "root_update", id=new_rev, topic="ROOT", expected_head=topic["revision"],
                         actor="ROOT.AUTHOR", source={"kind": "author_return", "id": returned, "reference": result["result_id"]})
                topic["revision"] = new_rev
                state["state"] = "delivered" if action == "deliver" else "quiescent"
                state["state_reason"] = output["reason"]
                topic["state"] = state["state"]
                state["deliveries"].append({"id": self._id(state, "DELIVERY"), "summary": output["summary"],
                     "state": state["state"], "source_job": spec["job_id"], "paths": files,
                     "reason": output["reason"], "unresolved": output["unresolved"]})
            else:
                revision = self.parliament._get(con, topic["revision"], "revision")
                self._returned(con, state, spec, {"ans": output["summary"], "con": output["reason"],
                    "contributions": [{"text": output["summary"], "candidate_ids": revision["candidates"]}]})
        elif action in {"wait", "checkpoint"}:
            if state.get("version", 3) >= 4:
                self._apply_wait(state, spec, output)
                if action == "checkpoint":
                    if not output["summary"].strip() or not output["reason"].strip() or not files:
                        raise WorkflowError("parent checkpoint requires a checkable partial artifact and boundary")
                    if topic["id"] == "ROOT":
                        state["deliveries"].append({"id":self._id(state,"DELIVERY"),"state":"partial",
                            "summary":output["summary"],"reason":output["reason"],"paths":files,
                            "source_job":spec["job_id"],"unresolved":output["unresolved"],
                            "remaining_dependencies":deepcopy(topic["wait_dependencies"])})
                    else:
                        revision = self.parliament._get(con, topic["revision"], "revision")
                        self._returned(con, state, spec, {"ans":output["summary"],"con":output["reason"],
                            "contributions":[{"text":output["summary"],"candidate_ids":revision["candidates"]}]}, partial=True)
            else:
                if not any(q["state"] == "open" for q in state["questions"].values()) and not any(t["state"] == "requested" for t in state["tool_requests"]) and not any(e["state"] in {"pending", "deferred"} for e in state["escalations"]):
                    raise WorkflowError("wait requires an actual question or external dependency")
                topic["state"] = "waiting"
                state["state"] = "waiting"
                state["state_reason"] = output["reason"]
        else:
            topic["state"] = "active"
            if (not self._pending(state) and not any(i["state"] == "pending" for i in state["schedule_intents"])
                    and not any(t.get("author_refresh_required") or t.get("dirty") for t in state["topics"].values())):
                state["state"] = "needs_attention"
                state["state_reason"] = "parent requested continue without executable work; explicit next action required"

    def input(self, text, question_id=None, correction=False, *, source="human", kind=None, topic_id=None, profile_scope="collaboration", attachments=None, submission_id=None):
        from .file_exchange import prepare
        files = prepare(attachments)
        if files and not text:
            text = "提交附件，请按本次文件版本处理。"
        if not isinstance(text, str) or not text.strip():
            raise WorkflowError("input text is required")
        if source == "user":
            source = "human"
        if source not in {"human", "operator", "fixture", "external"}:
            raise WorkflowError("explicit input source must be human/operator/fixture/external")
        fingerprint = digest({"text": text, "question": question_id, "topic": topic_id, "correction": correction,
            "source": source, "kind": kind, "profile_scope": profile_scope,
            "files": [{k: f[k] for k in ("name", "sha256", "replaces")} for f in files]})
        if submission_id is not None and (not isinstance(submission_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,100}', submission_id)):
            raise WorkflowError("submission_id must be an opaque 8..100 character identifier")
        with self._tx() as (con, state):
            prior = state.get("file_submissions", {}).get(submission_id) if submission_id else None
            if prior:
                if prior["sha256"] != fingerprint:
                    raise WorkflowError("submission_id 已对应其他内容；请创建新提交")
                return next(deepcopy(i) for i in state["user_inputs"] if i["id"] == prior["input_id"])
            question = state["questions"].get(question_id) if question_id else None
            if question_id and (question is None or question["state"] != "open"):
                raise WorkflowError("question is not currently open")
            if question and topic_id is not None and topic_id != question["topic_id"]:
                raise WorkflowError("a question answer must return to its originating topic")
            target = topic_id or (question["topic_id"] if question else "ROOT")
            if target not in state["topics"]:
                raise WorkflowError("input target topic does not exist")
            record = {"id": self._id(state, "INPUT"), "text": text, "source": source,
                      "kind": kind or ("correction" if correction else "answer" if question else "input"),
                      "topic_id": target, "question_id": question_id, "created_at": now()}
            self._receive_files(con, state, files, record)
            state["user_inputs"].append(record)
            if files:
                self._invalidate_file_waiters(state, record)
                self._file_dependency_changed(state, record)
            if submission_id:
                state.setdefault("file_submissions", {})[submission_id] = {"sha256": fingerprint, "input_id": record["id"]}
            if state.get("version", 3) >= 4 and source in {"human", "operator", "fixture"} and profile_scope != "none":
                from . import user_collaboration as user
                if profile_scope not in {"profile", "collaboration"}:
                    raise WorkflowError("unknown profile scope")
                presented = question_id and any(e["id"] == question_id + ".PRESENTED" for e in state["user_collaboration"]["events"])
                eid = record["id"] + ".USER"
                user.record_event(state, eid, "user_reply" if not question_id or presented else "observation", source=source,
                    topic_id=target, question_id=question_id if presented else None, content=text,
                    links=[question_id + ".PRESENTED"] if presented else [],
                    material_refs=question.get("guidance_ids", []) if question else [],
                    allowed_uses=["profile", "collaboration"] if profile_scope == "collaboration" else ["profile"],
                    audience=user.event_audience(state, ["*"] if profile_scope == "collaboration" else ["USER"],
                        links=[question_id + ".PRESENTED"] if presented else [],
                        material_refs=question.get("guidance_ids", []) if question else []))
                record["user_event_id"] = eid
            if question:
                question.update(state="answered", answer_id=record["id"])
                self._answer_dependencies(state, question, record)
                if question.get("disposition"):
                    d = self.parliament._get(con, question["disposition"], "disposition")
                    self._op(con, "resolve", id=self._id(state, "RESOLVE"), disposition=d["id"], receiver=d["target"],
                             outcome="handled", note="explicit input received: " + record["id"] + "; applicability still authored")
            # A correction never mutates a sealed in-flight prompt. Preserve its
            # late return and obtain fresh responsible reconsideration instead.
            if correction:
                affected = self._scope(state, target) if state.get("version", 3) >= 4 else set(state["topics"])
                if state.get("version", 3) < 4:
                    state["epoch"] += 1
                else:
                    for tid in affected:
                        state["topics"][tid]["epoch"] = state["topics"][tid].get("epoch", 0) + 1
                for job in state["jobs"].values():
                    if job["state"] in {"pending", "failed"} and job["spec"]["metadata"]["topic_id"] in affected:
                        job["state"] = "superseded"
                for inquiry in state["inquiries"].values():
                    if inquiry["state"] != "closed" and inquiry["topic_id"] in affected:
                        inquiry["state"] = "superseded"
                        state["obligations"].append({"id": self._id(state, "SUPERSEDED"), "kind": "superseded_inquiry",
                            "state": "conditional", "topic_id": inquiry["topic_id"], "request_id": inquiry["id"],
                            "candidate_ids": inquiry["candidates"], "source_input": record["id"],
                            "text": "Correction superseded sealed work; original candidates and questions remain historical, requiring fresh reconsideration rather than silent deletion."})
            current = state["topics"][target]
            current["state"] = "active"
            if target == "USER":
                if profile_scope == "none":
                    raise WorkflowError("input to USER must permit profile processing; use the original task for a profile opt-out")
                self._budgeted_schedule(con, state, "user_review", lambda: self._user_review(con, state))
            elif target != "ROOT":
                if not any(j["spec"]["role"] == "author" for j in self._pending(state, target)):
                    if state.get("version", 3) >= 4:
                        self._schedule_intent(state, "author", target)
                    else:
                        self._author(con, state, target)
                else:
                    current["author_refresh_required"] = True
            else:
                current["dirty"] = True
            state["state"] = "active"
            state["state_reason"] = "new explicitly sourced input requires fresh reconsideration"
            self._progress(con, state)
        self._export()
        return record

    def repair_context_delivery(self, return_ids, *, reason, selection_ids=(), source="operator"):
        """Append fresh evidence-bearing reviews; never edit historical inputs.

        This repairs transport of already-owned originals, not their semantic
        judgment. The normal parent receives the new reviews and decides next.
        Optional historical selection requests are explicitly reopened so their
        responsible parent can request actual re-delivery with current sources.
        """
        if source not in {"operator", "fixture"} or not isinstance(reason, str) or not reason.strip():
            raise WorkflowError("context repair requires an explicit operator/fixture reason")
        if not isinstance(return_ids, list) or len(return_ids) != len(set(return_ids)):
            raise WorkflowError("unique explicit return_ids are required")
        with self._tx() as (con, state):
            repair = {"id": self._id(state, "CONTEXT.REPAIR"), "reason": reason, "source": source,
                      "created_at": now(), "return_ids": return_ids, "selection_ids": list(selection_ids), "jobs": []}
            first_delivery = len(state["evidence_deliveries"])
            for rid in return_ids:
                returned = self.parliament._get(con, rid, "return")
                if returned["topic"] == "ROOT":
                    raise WorkflowError("repair targets child contributions, not root delivery")
                if any(r["state"] == "pending" and r["returned"] == rid for r in state["reassessments"].values()):
                    raise WorkflowError("this return already has a pending fresh evidence review")
                jid = self._return_source(con, state, rid)
                spec = deepcopy(state["jobs"][jid]["spec"])
                if spec["metadata"]["revision"] != state["topics"][returned["topic"]]["revision"]:
                    raise WorkflowError("historical revision requires explicit current parent reconsideration before new assessment")
                spec['metadata']['assessment_cycle'] = repair['id']
                jobs = self._schedule_assessments(con, state, spec, rid, returned)
                repair["jobs"].extend(jobs)
                state["reassessments"][repair["id"] + "." + rid] = {
                    "returned": rid, "jobs": jobs, "state": "pending", "repair_id": repair["id"]}
            for oid in selection_ids:
                obligation = next((o for o in state["obligations"] if o["id"] == oid and o.get("kind") == "selection"), None)
                if obligation is None:
                    raise WorkflowError("unknown selection request: " + oid)
                obligation.setdefault("delivery_repairs", []).append({"repair_id": repair["id"], "prior_state": obligation["state"],
                    "prior_response": obligation.get("response"), "reason": reason})
                obligation["state"] = "open"
                state["topics"][obligation["topic_id"]]["dirty"] = True
            repair["deliveries"] = state["evidence_deliveries"][first_delivery:]
            state["context_repairs"].append(repair)
            state["state"] = "active"
            state["state_reason"] = "actual stored originals sealed into fresh bounded assessments; old outcomes remain history"
            self._progress(con, state)
        return repair

    def reopen_parent_contributions(self, job_id, reason, source="operator"):
        """Reopen only a side-effect-free, current applied parent continuation.

        This explicit contract-damage repair preserves the original job, result,
        decision and consumed history. It neither rolls back structural actions
        nor substitutes an answer. Resume/advance seals a new parent input with
        the existing contributions and assessments plus the repair provenance.
        """
        def reject(message):
            raise WorkflowError(message + "; outside this restricted repair, use a checkpoint or explicit manual migration")

        if source not in {"operator", "fixture"} or not isinstance(reason, str) or not reason.strip():
            reject("parent contribution repair requires an explicit operator/fixture reason")
        with self._tx() as (con, state):
            if not state["paused"]:
                reject("parent contribution repair requires a paused workflow")
            if any(r["job_id"] == job_id for r in state["parent_contribution_repairs"]):
                reject("this parent job has already been reopened")
            if self._pending(state):
                reject("pending roles must finish before parent contribution repair")
            job = state["jobs"].get(job_id)
            if not job or job["state"] != "applied" or job["spec"]["role"] != "parent":
                reject("repair requires an applied parent job")
            spec, result_id = job["spec"], job.get("result_id")
            meta = spec["metadata"]
            topic = state["topics"].get(meta["topic_id"])
            if not topic or topic.get("last_decision", {}).get("job_id") != job_id:
                reject("repair target must be the current parent's last decision")
            if (meta["epoch"] != state["epoch"] or meta["revision"] != topic["revision"] or
                    self.parliament._head(con, topic["id"]) != meta["revision"]):
                reject("repair target must still belong to the current epoch and revision")
            row = con.execute("SELECT body,outcome FROM results WHERE result_id=?", (result_id,)).fetchone()
            if not row or row[1] != "applied":
                reject("repair requires the preserved applied original result")
            original = json.loads(row[0])
            output = original.get("payload")
            decision = topic["last_decision"]
            if (original.get("job_id") != job_id or decision.get("source_result") != result_id or
                    not isinstance(output, dict) or output.get("action") != "continue" or
                    decision.get("action") != "continue"):
                reject("only the current applied continue decision can be reopened")
            # close_request and late review occur even when output arrays are
            # empty. Unresolved entries also create persistent obligations.
            if any(meta.get(k) for k in ("candidate_ids", "request_ids", "selection_ids", "escalation_ids", "late_ids")):
                reject("parent metadata records structural dispositions, requests or late-result review")
            if any(output.get(k) for k in ("dispositions", "files", "questions", "tool_requests",
                                          "selection_responses", "escalation_responses", "unresolved")):
                reject("parent output contains structural side effects")
            if con.execute("SELECT 1 FROM artifacts WHERE source_job=? LIMIT 1", (job_id,)).fetchone() or any(
                    x.get("source_job") == job_id for x in
                    list(state["questions"].values()) + state["tool_requests"] + state["obligations"] + state["escalations"]):
                reject("parent job has persisted structural side effects")
            returned_ids = meta.get("return_ids")
            if (not isinstance(returned_ids, list) or not returned_ids or
                    any(not isinstance(r, str) for r in returned_ids) or len(set(returned_ids)) != len(returned_ids)):
                reject("repair requires unique original contribution return IDs")
            if sorted(d.get("return_id", "") for d in output.get("contribution_decisions", [])) != sorted(returned_ids):
                reject("original contribution decisions do not match the sealed return IDs")
            for rid in returned_ids:
                try:
                    returned = self.parliament._get(con, rid, "return")
                except ValueError:
                    reject("original contribution is missing: " + rid)
                child = state["topics"].get(returned["topic"])
                if (not child or child["parent"] != topic["id"] or
                        self.parliament._get(con, returned["topic"], "topic")["parent"] != topic["id"]):
                    reject("original contribution does not belong to this parent: " + rid)
                if (returned["revision"] != child["revision"] or
                        self.parliament._head(con, child["id"]) != returned["revision"]):
                    reject("original contribution no longer belongs to its current child revision: " + rid)
                if rid not in topic["consumed_returns"] or rid in topic["new_returns"]:
                    reject("original contribution must be consumed and not already reopened: " + rid)
            repair = {"id": self._id(state, "PARENT.REPAIR"), "kind": "parent_contribution_reopen",
                      "job_id": job_id, "result_id": result_id, "topic_id": topic["id"],
                      "revision": topic["revision"], "return_ids": list(returned_ids), "reason": reason,
                      "source": source, "created_at": now(), "original_result_sha256": hashlib.sha256(row[0].encode("utf-8")).hexdigest(),
                      "original_prompt_sha256": hashlib.sha256(spec["prompt"].encode("utf-8")).hexdigest(),
                      "decision_status": "voided_for_reconsideration_by_explicit_contract_repair",
                      "text": (f"{source} explicitly invalidates the semantic authority of parent decision {job_id} "
                               f"(result {result_id}) because of a mechanical output-contract defect: {reason}. "
                               "Its summary, reason and contribution decisions remain unmodified historical originals, "
                               "not reliable current decisions. Reconsider the reopened contributions using their existing "
                               "original artifacts and assessments. No replacement answer or acceptance is supplied by this repair.")}
            state["parent_contribution_repairs"].append(repair)
            state["user_inputs"].append(dict(repair))
            topic["new_returns"].extend(returned_ids)
            topic["dirty"] = True
            topic["state"] = "active"
            state["state"] = "active"
            state["state_reason"] = "explicit contract repair reopened consumed contributions; resume for fresh parent reconsideration"
        return repair

    def pause(self):
        with self._tx() as (_, state):
            state["paused"] = True
        return self.status()

    def tool_result(self, request_id, value, *, source="operator", defer_progress=False):
        """Receive explicitly executed adapter evidence; never execute its action."""
        if source not in {"operator", "external", "fixture"}:
            raise WorkflowError("tool result must disclose operator/external/fixture source")
        with self._tx() as (con, state):
            request = next((t for t in state["tool_requests"] if t["id"] == request_id), None)
            if request is None or request["state"] != "requested":
                raise WorkflowError("tool request is not awaiting a result")
            record = {"id": self._id(state, "INPUT"), "kind": "tool_result", "source": source,
                      "topic_id": request["topic_id"], "request_id": request_id, "value": value, "created_at": now()}
            state["user_inputs"].append(record)
            request.update(state="answered", result_id=record["id"])
            from .tool_recovery import receive_test
            receive_test(state, request, record)
            self._cognitive_delivery(state, request, record)
            self._web_delivery(state, request, record)
            if request.get("retry_of"):
                self._refresh_tool_owner(state, request["topic_id"], "Retried test returned: " + request_id)
            topic = state["topics"][request["topic_id"]]
            if topic["id"] != "ROOT":
                outstanding = any(t["topic_id"] == topic["id"] and t["state"] == "requested" for t in state["tool_requests"])
                if not outstanding:
                    topic["author_refresh_required"] = True
            else:
                topic["dirty"] = True
            state["state"] = "active"
            if not defer_progress:
                self._progress(con, state)
        return record

    def resume(self):
        with self._tx() as (_, state):
            state["paused"] = False
        return self.status()

    def retry(self, job_id):
        with self._tx() as (con, state):
            self._recognize_capacity_failures(state)
            old = state["jobs"].get(job_id)
            if not old or old["state"] not in {"failed","quarantined"}:
                raise WorkflowError("only a preserved failed or quarantined job can be retried")
            for batch in state.get("user_collaboration", {}).get("quarantined_batches", []):
                if batch["job_id"] == job_id:
                    batch["state"] = "reopened"
            spec = old["spec"]
            meta = {**spec["metadata"], "retry_of": job_id, "previous_error": old.get("error")}
            prompt = spec["prompt"]
            packet_error = self._input_packet_error(spec)
            if old.get("failure_kind") == "context_capacity" and spec["role"] in {"parent","author"}:
                self._request_capacity_rebuild(state, job_id)
                made = []
                self._budgeted_schedule(con, state, "capacity_retry:" + job_id,
                    lambda: made.append(self._parent(con, state, meta["topic_id"]) if spec["role"] == "parent"
                                        else self._author(con, state, meta["topic_id"])))
                return made[0] if made else None
            elif old.get("failure_kind") not in {"contract", "input_contract"} and not packet_error:
                # Transport faults say nothing about output quality. Keep the
                # exact sealed input rather than injecting a fictitious critique.
                new = self._job(state, spec["role"], meta["topic_id"], prompt, meta, schema=spec["schema"])
            elif spec["role"] in {"parent", "author"}:
                role = spec["role"]
                existing = next((j for j in self._pending(state, meta["topic_id"]) if j["spec"]["role"] == role), None)
                if existing:
                    old["state"] = "superseded"
                    self._replace_active_job(state, job_id, existing["spec"]["job_id"])
                    state["diagnostics"].append({"kind": "coalesced_" + role + "_retry", "job_id": job_id,
                        "replacement_job": existing["spec"]["job_id"], "reason": "fresh role already owns this topic; do not dispatch concurrent decisions"})
                    return existing["spec"]["job_id"]
                for rid in meta.get("request_ids", []):
                    state["inquiries"][rid]["state"] = "ready"
                old["state"] = "superseded"
                topic = state["topics"][meta["topic_id"]]
                if role == "parent":
                    topic["dirty"] = True
                else:
                    topic["author_refresh_required"] = True
                broken_input = bool(packet_error or old.get("failure_kind") == "input_contract")
                topic.setdefault("pending_role_repair", {})[role] = {"previous_job": job_id, "error": packet_error or old.get("error"),
                    "kind": "input_contract" if broken_input else "output_contract",
                    "instruction": ("The software supplied an inconsistent input contract. This task is rebuilt from authorized originals and outstanding duties; it is not a criticism of the previous answer. Rejected effects were not committed."
                        if broken_input else "Repair this explicit contract failure using the current state; rejected semantic effects were not committed.")}
                made = []
                def rebuild():
                    made.append(self._parent(con, state, meta["topic_id"]) if role == "parent" else self._author(con, state, meta["topic_id"]))
                    if role == "author":
                        state["topics"][meta["topic_id"]]["author_refresh_required"] = False
                self._budgeted_schedule(con, state, "contract_retry:" + job_id,
                    rebuild)
                if not made:
                    return None  # Preparation or a visible blocked scheduling intent survives.
                new = made[0]
            elif spec["role"] == "memory_review":
                previous = json.loads(prompt)
                prompt, schema, packet = self._memory_packet(previous["task"], validation_failure={
                    "previous_job": job_id, "error": old.get("error"),
                    "instruction": "Use the current action/status contract and repair this rejected output. The sealed task and input allowlist are unchanged."}, settings=context_policy(state))
                packet["previous_packet_sha256"] = hashlib.sha256(spec["prompt"].encode("utf-8")).hexdigest()
                meta["input_packet"] = packet
                new = self._job(state, spec["role"], meta["topic_id"], prompt, meta, schema=schema)
            else:
                if spec["role"] in {"author", "member", "assessment", "user_review", "memory_review", "processing", "context_summary", "context_review"}:
                    repair = json.loads(prompt)
                    repair["task"]["validation_failure"] = {"previous_job": job_id, "error": old.get("error"),
                        "instruction": "Correct the invalid shape, source attribution or oversized contribution; preserve full artifacts in files."}
                    prompt = encode(repair)
                new = self._job(state, spec["role"], meta["topic_id"], prompt, meta, schema=spec["schema"])
            old["state"] = "superseded"
            old.pop("retry_at", None)
            self._replace_active_job(state, job_id, new)
            state["state"] = "active"
        return new

    def repair_contract_failures(self):
        """At most two fresh samples per rejected contract lineage, never replay uncertain execution."""
        eligible = []
        with self._tx() as (con, state):
            if state.get("version", 3) < 4 or state["paused"]:
                return []
            for jid, job in state["jobs"].items():
                if job["state"] != "failed" or not job.get("result_id"):
                    continue
                row = con.execute("SELECT outcome FROM results WHERE result_id=?", (job["result_id"],)).fetchone()
                if job.get("uncertain"):
                    continue
                if not row or (row[0] != "rejected" and not (job.get("failure_kind") in {"contract", "input_contract"} and row[0] == "failed")):
                    continue
                if self._input_packet_error(job["spec"]) or job.get("failure_kind") == "input_contract":
                    # Repair the software's packet even if the old model-output
                    # retries were exhausted. _job preflight forbids redispatch
                    # until the new packet is structurally valid.
                    eligible.append(jid)
                    continue
                if any(token in str(job.get("error", "")) for token in ("exceeds bound", "exceed bound", "exceeds one bounded", "projection metadata overflow")):
                    if not any(d.get("job_id") == jid for d in state["diagnostics"]):
                        state["diagnostics"].append({"job_id": jid, "kind": "input_restructuring_required", "error": job["error"],
                            "action": "split the actual task or explicitly restructure constraints; resampling cannot enlarge the input boundary"})
                    continue
                depth, cursor = 0, job
                while cursor["spec"]["metadata"].get("retry_of") in state["jobs"]:
                    if cursor["spec"]["metadata"].get("repair_kind") == "input_contract":
                        break
                    cursor = state["jobs"][cursor["spec"]["metadata"]["retry_of"]]
                    if cursor.get("failure_kind") == "contract":
                        depth += 1
                if depth < 2:
                    eligible.append(jid)
                elif not any(d.get("job_id") == jid for d in state["diagnostics"]):
                    state["diagnostics"].append({"job_id": jid, "kind": "contract_repair_exhausted", "error": job.get("error"),
                        "action": "inspect preserved input/output; fix contract or task, then explicitly retry", "attempts": depth})
        repaired = []
        for jid in eligible:
            new = self.retry(jid)
            with self._tx() as (_, state):
                broken = self._input_packet_error(state["jobs"][jid]["spec"]) or state["jobs"][jid].get("failure_kind") == "input_contract"
                state["diagnostics"].append({"kind": "input_contract_rebuilt" if broken else "contract_resampled", "job_id": jid, "replacement_job": new,
                    "error": state["jobs"][jid].get("error"), "boundary": "rejected transaction rolled back; original retained; no semantic acceptance supplied"})
            repaired.append(new)
        return repaired

    def status(self, *, compact=False):
        with self._read_tx() as (con, state):
            from . import user_collaboration as user
            from .status_view import headers, input_views, memory_view
            from .tool_recovery import test_status
            return {"run_id": state["run_id"], "goal": state["goal"],
                    "state": "paused" if state["paused"] else state["state"], "paused": state["paused"],
                    "state_reason": state["state_reason"], "topics": list(state["topics"].values()),
                    "active_topics": [t for t in state["topics"].values() if t["state"] not in {"returned", "delivered", "quiescent", "sleeping"}],
                    "questions": list(state["questions"].values()),
                    "jobs": [{"job_id": jid, "role": j["spec"]["role"], "topic_id": j["spec"]["metadata"]["topic_id"],
                              "state": j["state"], "error": j.get("error"), "uncertain":j.get("uncertain",False),
                              "failure_kind":j.get("failure_kind"), "retry_at":j.get("retry_at")} for jid, j in state["jobs"].items()],
                    "pending_semantic_work": len(self._pending(state)) + sum(i["state"] == "pending" for i in state["schedule_intents"]),
                    "schedule_intents": headers(state['schedule_intents'],('id','kind','topic_id','revision','state')) if compact else state["schedule_intents"], "unresolved_obligations": state["obligations"],
                    "deliveries": state["deliveries"], "delivery_paths": [r[0] for r in con.execute("SELECT path FROM artifacts")],
                    "experience": {"learning_count": con.execute("SELECT count(*) FROM objects WHERE kind='learning'").fetchone()[0],
                                    "materials": [r[0] for r in con.execute("SELECT id FROM objects WHERE kind='material' AND json_extract(body,'$.form')='synthesis'")]},
                    "user_inputs": input_views(state['user_inputs']) if compact else state["user_inputs"],
                    "tool_requests": headers(state['tool_requests'],('id','kind','topic_id','source_job','state','purpose','result_id','retry_of','retried_by')) if compact else state['tool_requests'],
                    "python_tests":test_status(state),
                    "sandbox_check": state.get("sandbox_check"), "input_receipts": state.get("input_receipts",{}),
                    "late_results": state["late_results"], "inquiries": list(state["inquiries"].values()),
                    "escalations": state["escalations"],
                    "evidence_deliveries": headers(state['evidence_deliveries'],('request_id','returned','member_id','state','result_ids','uptake_job_ids')) if compact else state["evidence_deliveries"], "context_repairs": state["context_repairs"],
                    "parent_contribution_repairs": state["parent_contribution_repairs"],
                    "version": state.get("version", 3), "diagnostics": state["diagnostics"],
                    "input_status":self._input_status(state,compact=compact), "context_memory":self._context_status(state),
                    "question_handoffs":deepcopy(state.get("question_handoffs", [])),
                    "internal_requests":deepcopy(state.get("internal_requests", [])),
                    "working_knowledge":state.get('working_knowledge',{}),
                    "automatic_retries_pending":any(j["state"] == "failed" and j.get("retry_at") for j in state["jobs"].values()),
                    "user_collaboration": user.status(state) if "user_collaboration" in state else None,
                    "user_assets": list(state.get("user_collaboration", {}).get("assets", {}).values()),
                    "user_events": headers(state.get('user_collaboration',{}).get('events',[]),('id','kind','source','topic_id','question_id')) if compact else state.get("user_collaboration", {}).get("events", []),
                    "user_material_deliveries": state.get("user_material_deliveries", []),
                    "memory": memory_view(state.get('memory',{})) if compact else state.get("memory", {}),
                    "cognitive_operations": headers(state.get('cognitive_operations',{}).values(),('id','operation','topic_id','state','active_job')) if compact else deepcopy(state.get("cognitive_operations", {})),
                    "web_research": [{"id": p["id"], "topic_id": p["topic_id"], "state": p["state"],
                        "active_job": p["active_job"], "input_id": p.get("input_id"),
                        "consumption": deepcopy(p["consumption"])} for p in state.get("web_research", {}).values()],
                    "file_exchange": list(deepcopy(state.get("file_exchange", {})).values()),
                    "boundary": "Fresh explicit calls and full field character budgets; hidden host isolation and cognitive causal benefit are not established."}

    def _export(self):
        """Idempotently publish artifacts already committed in SQLite."""
        with closing(sqlite3.connect(self.db)) as con:
            records = list(con.execute("SELECT path,content FROM artifacts WHERE path NOT IN (SELECT path FROM artifact_publications)"))
        for relative, content in records:
            path = (self.path / relative).resolve()
            boundary = (self.path / "artifacts").resolve()
            if not boundary.is_relative_to(self.path) or not path.is_relative_to(boundary) or path == boundary:
                raise WorkflowError("artifact publication escaped run directory")
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.is_file() or path.read_text(encoding="utf-8") != content:
                tmp = path.with_name(path.name + ".publishing")
                tmp.write_text(content, encoding="utf-8", newline="\n")
                tmp.replace(path)
            with closing(sqlite3.connect(self.db)) as con, con:
                con.execute("INSERT OR IGNORE INTO artifact_publications VALUES(?)", (relative,))
