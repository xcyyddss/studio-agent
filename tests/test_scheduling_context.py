"""Capacity, finite feedback cohorts and persistent dispatch controls.

Synthetic fixtures only. These tests make no model calls.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_runtime.context import ordinary_input, project_context, share_exact_bodies
from studio_runtime.context_budget import capacity, text_tokens, tokenizer
from studio_runtime.context_lifecycle import ContextLifecycle, ContextPending, DEFAULT_POLICY, memory_body
from studio_runtime.contracts import canonical, JobSpec, digest
from studio_runtime.feedback import open_wave, parent_ready
from studio_runtime.service import StudioService, validate_workers
from studio_runtime.workflow import Workflow
from studio_runtime.queue import QueueStore
from test_input_lifecycle import empty, output


class MemoryHarness(ContextLifecycle):
    def _job(self, state, role, topic, prompt, metadata=None, schema=None):
        jid = "J" + str(len(state["jobs"]))
        state["jobs"][jid] = {"state":"pending", "spec":{"role":role, "prompt":prompt, "metadata":metadata}}
        return jid

    def complete(self, state, summarize=None, accepted=True):
        for jid, job in list(state["jobs"].items()):
            if job["state"] != "pending":
                continue
            spec = job["spec"]
            task = json.loads(spec["prompt"])["task"]
            if spec["role"] == "context_summary":
                source = task["sources"][0]
                out = dict(summary=summarize(source["text"]) if summarize else "Limit 17; alternatives remain open.",
                    constraints=["Limit 17"], decisions=[], conflicts=[], open_questions=[], early_directions=[],
                    exact_quotes=[], coverage=[{k:source[k] for k in ("source_id","start","end")} |
                        dict(treatment="consolidated", reason="Synthetic fixture coverage")])
                self._apply_context_summary(None, state, spec, out, {"result_id":jid})
            else:
                self._apply_context_review(None, state, spec,
                    dict(accepted=accepted, missing=[] if accepted else ["constraint"], distorted=[],
                         unsupported=[], missing_constraints=[], reason="Synthetic review"),
                    {"result_id":jid})
            job["state"] = "applied"


def memory_state(**settings):
    return {"metadata":{"context_policy":settings}, "jobs":{}, "context_snapshots":{}, "topics":{}}


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.h = MemoryHarness()

    def compact(self, state, body, *, target=1000, fits=None, summarize=None):
        for _ in range(50):
            try:
                return self.h._compact_text(state, "ROOT", "parent:history", body, target, fits=fits)
            except ContextPending:
                self.h.complete(state, summarize)
        self.fail("memory preparation did not settle")

    def test_cache_reuses_exact_body_despite_unrelated_lineage_but_not_scope(self):
        s = memory_state()
        a = self.h._memory_unit(s, "ROOT", "parent", "Original complete text", children=["A"], level=1)
        b = self.h._memory_unit(s, "ROOT", "parent", "Original complete text", children=["A", "unrelated"], level=1)
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(len(s["jobs"]), 1)
        c = self.h._memory_unit(s, "OTHER", "parent", "Original complete text", children=["A"])
        self.assertNotEqual(a["id"], c["id"])

    def test_cache_invalidates_when_actual_cognitive_protocol_changes(self):
        from studio_runtime.cognitive_protocols import bundle
        s = memory_state()
        old = self.h._memory_unit(s, "ROOT", "parent", "Retain this exact source")
        def changed(role):
            result = bundle(role)
            if role == "context_review":
                result["sha256"] = "changed-review-protocol"
            return result
        with patch("studio_runtime.context_lifecycle.bundle", side_effect=changed):
            new = self.h._memory_unit(s, "ROOT", "parent", "Retain this exact source")
        self.assertNotEqual(old["id"], new["id"])

    def test_append_preserves_last_complete_block_and_separator_exactly(self):
        s = memory_state(source_chunk_chars=4000, summary_target_chars=500)
        original = "Limit 17. " + "Old paragraph. " * 180
        self.compact(s, original)
        first_ids = set(s["context_memory"]["units"])
        self.compact(s, original + "\n" + "A new complete record. " * 120)
        units = s["context_memory"]["units"]
        originals = [u for u in units.values() if u["source"]["text"] == original]
        self.assertEqual(len(originals), 1)
        self.assertTrue(first_ids <= set(units))
        self.assertFalse(any(u["source"]["text"] == original + "\n" for u in units.values()))
        self.assertTrue(all(p["covered"] for p in s["context_memory"]["plans"].values()))

    def test_stop_as_soon_as_whole_call_fits_not_tiny_memory_target(self):
        s = memory_state(source_chunk_chars=6000, summary_target_chars=2500)
        body = "\n".join("Record " + str(i) + ": " + "independent fact " * 270 for i in range(4))
        result = self.compact(s, body, target=300,
            fits=lambda candidate: len(candidate) < 12000,
            summarize=lambda source: source[:1800])
        self.assertGreater(len(result[0]), 300)
        self.assertLess(len(result[0]), 12000)
        self.assertTrue(all(u["level"] == 0 for u in s["context_memory"]["units"].values()))
        calls = len(s["jobs"])
        self.compact(s, body, target=300, fits=lambda candidate: len(candidate) < 12000)
        self.assertEqual(len(s["jobs"]), calls)

    def test_inflating_summary_keeps_original_and_negative_result_is_cached(self):
        s = memory_state()
        source = "Required fact. " * 2000
        first = self.compact(s, source, summarize=lambda body: body + "Unsupported filler. " * 100)
        self.assertEqual(first[0], source)
        self.assertTrue(all(u["state"] == "no_gain" for u in s["context_memory"]["units"].values()))
        jobs = len(s["jobs"])
        self.assertEqual(self.compact(s, source)[0], source)
        self.assertEqual(len(s["jobs"]), jobs)

    def test_rejected_summary_never_replaces_original(self):
        s = memory_state()
        body = "Protected original. " * 1000
        for _ in range(20):
            try:
                result = self.h._compact_text(s, "ROOT", "parent", body, 1000)
                break
            except ContextPending:
                self.h.complete(s, accepted=False)
        self.assertEqual(result[0], body)
        self.assertTrue(all(u["state"] == "uncompressible" for u in s["context_memory"]["units"].values()))

    def test_duplicate_delivery_keeps_body_provenance_and_current_inputs(self):
        body = "A complete original body. " * 500
        task = {"topic":"ROOT", "user_inputs":[{"id":"I", "text":body}], "input_obligations":["I"],
            "artifacts":[{"path":"a.txt", "content":body, "source_job":"J1"}],
            "public_evidence":{"artifacts":[{"id":"a.txt", "content":body, "source_job":"J1"}]},
            "work_history":[{"output":{"files":[{"path":"a.txt", "content":body}]}}]}
        shared = share_exact_bodies(task)
        self.assertEqual(shared["user_inputs"], task["user_inputs"])
        self.assertEqual(len(shared["shared_bodies"]), 1)
        self.assertEqual(next(iter(shared["shared_bodies"].values()))["body"], body)
        self.assertEqual(shared["artifacts"][0]["source_job"], "J1")
        self.assertLess(len(canonical(shared)), len(canonical(task)))
        self.assertEqual(task["artifacts"][0]["content"], body)

    def test_capacity_uses_complete_envelope_and_reports_uncertainty(self):
        result = capacity("A short prompt", {"type":"object"})
        self.assertIn(result["method"], {"model_mapping", "o200k_base_proxy", "utf8_bytes_upper_bound"})
        self.assertGreater(result["reserved_tokens"], 0)
        self.assertEqual(result["context_tokens"], 872000)
        self.assertIn("not exact", result["boundary"])
        with patch("studio_runtime.context_budget.tokenizer", return_value=(None, "utf8_bytes_upper_bound")):
            text_tokens.cache_clear()
            self.assertEqual(text_tokens("中文☃")[0], len("中文☃".encode("utf-8")))
        text_tokens.cache_clear()

    def test_tokenizer_is_offline_with_empty_cache_and_missing_or_bad_assets(self):
        import tiktoken
        tokenizer.cache_clear()
        text_tokens.cache_clear()
        try:
            with tempfile.TemporaryDirectory() as folder:
                # Prohibit both implicit tiktoken downloads and all network.
                with patch.dict("os.environ", {"TIKTOKEN_CACHE_DIR":folder}), \
                     patch.object(tiktoken, "get_encoding", side_effect=AssertionError("implicit download")), \
                     patch("socket.socket", side_effect=AssertionError("network forbidden")):
                    codec, method = tokenizer()
                    self.assertEqual(method, "o200k_base_proxy")
                    self.assertEqual(codec.decode(codec.encode("完整中文 and English 17")), "完整中文 and English 17")
                    self.assertEqual(list(Path(folder).iterdir()), [])
                with patch("studio_runtime.context_budget.TOKENIZER_DATA", Path(folder)):
                    tokenizer.cache_clear()
                    self.assertEqual(tokenizer(), (None, "utf8_bytes_upper_bound"))
                    (Path(folder) / "o200k_base.tiktoken").write_bytes(b"corrupt table")
                    tokenizer.cache_clear()
                    self.assertEqual(tokenizer(), (None, "utf8_bytes_upper_bound"))
        finally:
            tokenizer.cache_clear()
            text_tokens.cache_clear()


def feedback_fixture():
    topics = {tid: {"id":tid, "parent":None if tid=="ROOT" else "ROOT", "revision":tid+".v1",
        "state":"active", "new_returns":[], "consumed_returns":[]} for tid in ("ROOT", "A", "B", "OTHER")}
    return dict(topics=topics, jobs={}, questions={}, inquiries={}, user_inputs=[], escalations=[],
                input_receipts={}, return_sources={}, diagnostics=[])


def returned(s, tid):
    rid = "R." + tid
    s["topics"][tid]["awaiting_feedback"] = [rid]
    s["return_sources"][rid] = "J." + tid
    s["jobs"]["J."+tid] = {"state":"applied", "spec":{"metadata":{"topic_id":tid,"revision":tid+".v1"}}}
    s["topics"]["ROOT"]["new_returns"].append(rid)


class FeedbackTests(unittest.TestCase):
    def test_first_return_waits_second_flushes_unrelated_child_does_not_gate(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        returned(s, "A")
        self.assertFalse(parent_ready(s, "ROOT"))
        returned(s, "B")
        self.assertTrue(parent_ready(s, "ROOT"))
        self.assertEqual(s["topics"]["ROOT"]["feedback_wait"]["reason"], "cohort_ready")

    def test_partial_blocked_wave_keeps_missing_responsibility(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        returned(s, "A")
        s["questions"]["Q"] = dict(id="Q",topic_id="B",state="open",blocking=True)
        self.assertTrue(parent_ready(s, "ROOT"))
        waiting = s["topics"]["ROOT"]["feedback_wait"]
        self.assertEqual(waiting["reason"], "blocked_partial")
        self.assertEqual(waiting["waves"][0]["participants"][1]["blockers"], ["Q"])
        s["topics"]["ROOT"]["new_returns"].clear()
        s["topics"]["ROOT"]["consumed_returns"].append("R.A")
        s["questions"]["Q"]["state"] = "answered"
        self.assertFalse(parent_ready(s, "ROOT"))
        returned(s, "B")
        self.assertTrue(parent_ready(s, "ROOT"))

    def test_correction_and_urgent_notice_bypass_wait_without_mutating_wave(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        s["user_inputs"] = [{"id":"INPUT", "topic_id":"ROOT"}]
        self.assertTrue(parent_ready(s, "ROOT"))
        s["input_receipts"]["INPUT"] = {"state":"processed"}
        self.assertFalse(parent_ready(s, "ROOT"))
        s["topics"]["ROOT"]["feedback_notices"] = [{"source_job":"J", "reason":"counterevidence"}]
        self.assertTrue(parent_ready(s, "ROOT"))
        self.assertEqual(set(s["topics"]["ROOT"]["feedback_waves"][0]["expected"]), {"A","B"})

    def test_revision_supersedes_one_slot_and_wave_survives_restart(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        s = json.loads(json.dumps(s))
        s["topics"]["A"]["revision"] = "A.v2"
        returned(s, "B")
        self.assertTrue(parent_ready(s, "ROOT"))
        self.assertEqual(s["topics"]["ROOT"]["feedback_wait"]["waves"][0]["participants"][0]["status"], "superseded")

    def test_ready_inquiry_unblocks_parent_instead_of_waiting_for_own_descendant(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        s["inquiries"]["I"] = dict(id="I", topic_id="ROOT", state="ready")
        self.assertTrue(parent_ready(s, "ROOT"))

    def test_known_block_and_waiting_input_do_not_repeatedly_interrupt_cohort(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B", "OTHER"])
        returned(s, "A")
        s["questions"]["Q"] = dict(id="Q",topic_id="B",state="open",blocking=True)
        self.assertTrue(parent_ready(s, "ROOT"))
        snapshot = deepcopy(s["topics"]["ROOT"]["feedback_wait"])
        s["jobs"]["P2"] = {"state":"applied", "spec":{"metadata":{"topic_id":"ROOT","feedback_batch":snapshot}}}
        s["topics"]["ROOT"]["last_decision"] = {"job_id":"P2"}
        s["user_inputs"] = [{"id":"ANSWER", "topic_id":"ROOT"}]
        s["input_receipts"]["ANSWER"] = {"state":"followup", "job_id":"P2"}
        self.assertFalse(parent_ready(s, "ROOT"))
        returned(s, "OTHER")
        self.assertTrue(parent_ready(s, "ROOT"))

    def test_scheduling_block_is_identified_through_its_intent_not_topic_name(self):
        s = feedback_fixture()
        open_wave(s, "ROOT", "P1", ["A", "B"])
        returned(s, "A")
        s["schedule_intents"] = [dict(id="S17",topic_id="B",state="pending")]
        s["diagnostics"] = [dict(stage="intent:S17",status="blocked")]
        self.assertTrue(parent_ready(s, "ROOT"))
        self.assertEqual(s["topics"]["ROOT"]["feedback_wait"]["waves"][0]["participants"][1]["blockers"], ["intent:S17"])


class IntegrationTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".validation"
        base.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.tmp.cleanup)
        self.service = StudioService(self.tmp.name)
        self.addCleanup(self.service.close)

    def test_default_six_persists_and_lowering_does_not_cancel_six_active_claims(self):
        self.service.create("Concurrency fixture", run_id="sample")
        path = self.service.path("sample")
        _, queue, executor = self.service._components(path)
        self.assertEqual(executor.max_workers, 6)
        for n in range(8):
            queue.enqueue(JobSpec(job_id="J"+str(n),run_id="sample",kind="fixture",role="fixture",
                prompt="{}",schema={},metadata={}))
        self.assertEqual(sum(queue.claim("fixture") is not None for _ in range(8)), 6)
        self.service.configure_scheduling("sample", 2)
        other = StudioService(self.tmp.name, max_workers=12)
        self.addCleanup(other.close)
        _, _, after = other._components(path)
        self.assertEqual(after.max_workers, 2)
        with patch("studio_runtime.worker.subprocess.Popen") as launch:
            after.tick("sample")
            launch.assert_not_called()
        self.assertEqual(sum(a["state"] == "leased" for a in queue.attempts()), 6)
        self.service.configure_scheduling("sample", 8)
        _, _, increased = other._components(path)
        self.assertEqual(increased.max_workers, 8)
        self.assertIsNotNone(queue.claim("fixture", max_active=increased.max_workers))
        for invalid in (True, 0, 33, 2.5, "6"):
            with self.assertRaises(ValueError): self.service.configure_scheduling("sample", invalid)

    def test_service_imports_available_results_before_progress_and_acknowledges_each(self):
        self.service.create("Queue batch fixture", run_id="sample")
        path = self.service.path("sample")
        wf, queue, _ = self.service._components(path)
        for job in wf.advance():
            queue.enqueue(job)
        count = 0
        while (claim := queue.claim("fixture")) is not None:
            job = claim["job"]
            queue.begin_attempt(claim["attempt_id"], 999)
            result = dict(job_id=job["job_id"], attempt_id=claim["attempt_id"], outcome="succeeded",
                payload={"candidates":[], "selection_requests":[], "outcome":"no_new_direction", "reason":"Synthetic generation has no new direction"}, error=None,
                receipt={"evidence_kind":"fixture", "job_sha256":digest(job)})
            result["result_sha256"] = digest(result)
            queue.complete(claim["attempt_id"], result)
            count += 1
        imported = []
        real_apply = Workflow.apply
        real_progress = Workflow._progress
        observations = []
        def apply(w, result, *args, **kwargs):
            self.assertTrue(kwargs["defer_progress"])
            response = real_apply(w, result, *args, **kwargs)
            imported.append(result["job_id"])
            return response
        def progress(w, con, state):
            observations.append(len(imported))
            return real_progress(w, con, state)
        with patch.object(Workflow, "apply", apply), patch.object(Workflow, "_progress", progress), patch("studio_runtime.worker.Executor.tick"):
            self.service.tick("sample")
        self.assertEqual(len(imported), count)
        self.assertTrue(observations)
        self.assertTrue(all(n == count for n in observations))
        self.assertEqual(queue.pending_results(), [])

    def test_large_but_fitting_history_does_not_schedule_summary(self):
        self.service.create("History fixture", run_id="sample")
        wf = Workflow.open(self.service.path("sample"))
        with wf._tx() as (con, state):
            for j in state["jobs"].values(): j["state"] = "superseded"
            for i in state["inquiries"].values(): i["state"] = "closed"
            state["paused"] = True
            # Matches the old 34k-character trigger, while comfortably fitting.
            for n in range(4):
                iid = "OLD"+str(n)
                state["user_inputs"].append({"id":iid, "topic_id":"ROOT", "kind":"input", "text":"Early abstract alternative. " * 330})
                state["input_receipts"][iid] = {"state":"processed"}
            jid = wf._parent(con, state, "ROOT")
            spec = state["jobs"][jid]["spec"]
            self.assertTrue(spec["metadata"]["capacity"]["fits"])
            self.assertFalse(any(j["spec"]["role"] in {"context_summary","context_review"} for j in state["jobs"].values()))
            self.assertNotIn("history_memory", json.loads(spec["prompt"])["task"])

    def test_service_merges_immediately_available_tool_results_before_parent(self):
        self.service.create("Compare the two requested fixture sources", run_id="tools")
        wf = Workflow.open(self.service.path("tools"))
        for job in wf.advance():
            if job.role == "member":
                self.assertEqual(wf.apply(job.job_id, {"candidates":[], "selection_requests":[], "outcome":"no_new_direction", "reason":"Synthetic generation has no new direction"})["status"], "applied")
        parent = next(j for j in wf.advance() if j.role == "parent")
        answer = output(parent.to_dict(), action="wait")
        answer["tool_requests"] = [dict(kind="fixture.read", arguments="{}", purpose="Synthetic source "+str(i)) for i in range(2)]
        self.assertEqual(wf.apply(parent.job_id, answer)["status"], "applied")
        with patch("studio_runtime.tool_adapters.LocalToolAdapter.supports", return_value=True), \
             patch("studio_runtime.tool_adapters.LocalToolAdapter.execute",
                   side_effect=lambda request: {"outcome":"succeeded", "payload":"Source "+request["id"]}), \
             patch("studio_runtime.worker.Executor.tick"):
            self.service.tick("tools")
        with wf._tx() as (_, s):
            pending = [j["spec"] for j in s["jobs"].values() if j["state"]=="pending" and j["spec"]["role"]=="parent"]
            self.assertEqual(len(pending), 1)
            task = json.loads(pending[0]["prompt"])["task"]
            self.assertEqual(len(task["input_obligations"]), 2)
            self.assertTrue(all(r["state"] == "answered" for r in task["tool_requests"]))

    def test_user_administration_alone_does_not_wake_root(self):
        self.service.create("Private collaboration fixture", run_id="sample")
        wf = Workflow.open(self.service.path("sample"))
        with wf._tx() as (_, s):
            for j in s["jobs"].values(): j["state"] = "superseded"
            for i in s["inquiries"].values(): i["state"] = "closed"
            s["paused"] = True
            s["topics"]["ROOT"]["dirty"] = False
        wf.input("A private collaboration observation", topic_id="USER", profile_scope="profile")
        with wf._tx() as (_, s):
            job = next(j["spec"] for j in s["jobs"].values() if j["state"]=="pending" and j["spec"]["role"]=="user_review")
        out = empty(job["schema"])
        out.update(processed_event_ids=job["metadata"]["user_context"]["pending_event_ids"], summary="No new general guidance")
        self.assertEqual(wf.apply(job["job_id"], out)["status"], "applied")
        with wf._tx() as (_, s):
            self.assertFalse(s["topics"]["ROOT"]["dirty"])

    def test_real_workflow_two_children_assessed_before_single_parent_flush(self):
        self.service.create("Compare two independent design directions", run_id="wave")
        wf = Workflow.open(self.service.path("wave"))
        def state():
            with wf._tx() as (_, s): return deepcopy(s)
        initial = state()
        for job in initial["jobs"].values():
            spec = job["spec"]
            if spec["role"] != "member": continue
            out = {"candidates":[{"id":spec["metadata"]["candidate_prefix"]+"1",
                "q":"Investigate one independently scoped design direction",
                "I":[], "source_note":"Synthetic fixture; no empirical claim"}], "selection_requests":[], "outcome":"proposed", "reason":"Synthetic distinct direction"}
            self.assertEqual(wf.apply(spec["job_id"], out, defer_progress=True)["status"], "applied")
        wf.advance()
        parent = next(j["spec"] for j in state()["jobs"].values() if j["state"]=="pending" and j["spec"]["role"]=="parent")
        out = output(parent)
        out["dispositions"] = [dict(candidate_id=cid, action="adopt", reason="Independent fixture work",
            replacement="", relation="complement") for cid in parent["metadata"]["candidate_ids"]]
        self.assertEqual(wf.apply(parent["job_id"], out)["status"], "applied")
        authors = [j["spec"] for j in state()["jobs"].values() if j["state"]=="pending" and j["spec"]["role"]=="author"]
        self.assertEqual(len(authors), 2)
        wave = state()["topics"]["ROOT"]["feedback_waves"][0]
        self.assertEqual(len(wave["expected"]), 2)
        for index, spec in enumerate(authors):
            out = empty(spec["schema"])
            task = json.loads(spec["prompt"])["task"]
            out.update(status="complete", ans="Synthetic result", con="A useful direction to compare",
                contributions=[dict(text="Synthetic finding", candidate_ids=task["revision"]["candidates"])])
            self.assertEqual(wf.apply(spec["job_id"], out)["status"], "applied")
            for job in state()["jobs"].values():
                assessment = job["spec"]
                if job["state"]=="pending" and assessment["role"]=="assessment" and assessment["metadata"]["topic_id"]==spec["metadata"]["topic_id"]:
                    answer = empty(assessment["schema"])
                    answer.update(judgment="sufficient", reason="Fixture claim is explicitly synthetic",
                        abstract="Compare independent directions", scenario="Synthetic fixture", conditions="Not empirical")
                    self.assertEqual(wf.apply(assessment["job_id"], answer)["status"], "applied")
            fresh = [j["spec"] for j in state()["jobs"].values() if j["state"]=="pending" and j["spec"]["role"]=="parent"
                     and j["spec"]["metadata"]["topic_id"]=="ROOT"]
            self.assertEqual(len(fresh), index)
        final = fresh[0]
        self.assertEqual(len(final["metadata"]["return_ids"]), 2)
        self.assertEqual(final["metadata"]["feedback_batch"]["reason"], "cohort_ready")
        out = output(final)
        out["contribution_decisions"] = [dict(return_id=rid, decision="retain", reason="Both fixture contributions considered")
                                        for rid in final["metadata"]["return_ids"]]
        self.assertEqual(wf.apply(final["job_id"], out)["status"], "applied")
        self.assertEqual(set(state()["topics"]["ROOT"]["consumed_returns"]), set(final["metadata"]["return_ids"]))
        self.assertEqual(state()["topics"]["ROOT"]["new_returns"], [])

    def test_successful_capacity_rebuild_clears_temporary_pressure(self):
        self.service.create("Recovery continuation fixture", run_id="recovered")
        w = Workflow.open(self.service.path("recovered"))
        with w._tx() as (con, s):
            for j in s["jobs"].values(): j["state"]="superseded"
            for i in s["inquiries"].values(): i["state"]="closed"
            s["paused"] = True
            jid = w._parent(con, s, "ROOT")
            spec = deepcopy(s["jobs"][jid]["spec"])
            s["topics"]["ROOT"]["context_pressure"] = {"job_id":"FAILED", "target_text_tokens":1000}
        self.assertEqual(w.apply(jid, output(spec))["status"], "applied")
        with w._tx() as (con, s):
            self.assertNotIn("context_pressure", s["topics"]["ROOT"])
            later = w._parent(con, s, "ROOT")
            self.assertNotIn("history_memory", json.loads(s["jobs"][later]["spec"]["prompt"])["task"])
            self.assertFalse(s.get("context_memory", {}).get("units", {}))

    def test_child_parent_packet_does_not_pool_sibling_artifact_body(self):
        self.service.create("Scoped originals fixture", run_id="scope")
        w = Workflow.open(self.service.path("scope"))
        with w._tx() as (con, s):
            for j in s["jobs"].values(): j["state"]="superseded"
            for i in s["inquiries"].values(): i["state"]="closed"
            s["paused"]=True
            # Two independent fixture topics with valid persisted revisions.
            for name in ("A","B"):
                w.parliament._put(con, name, "topic", parent="ROOT", actor=name+".AUTHOR")
                w.parliament._put(con, name+".v1", "revision", topic=name,parent="ROOT",questions=["Local work"],
                    I=[],candidates=[],previous=None,continuity="fresh",unresolved_previous="",relation="complement")
                con.execute("INSERT INTO heads VALUES(?,?)",(name,name+".v1"))
                s["topics"][name] = dict(id=name,parent="ROOT",revision=name+".v1",state="active",summary="",
                    new_returns=[],consumed_returns=[],dirty=False)
                s["jobs"]["SOURCE."+name]={"state":"applied","spec":{"job_id":"SOURCE."+name,"role":"author",
                    "metadata":{"topic_id":name,"revision":name+".v1"}}}
                con.execute("INSERT INTO artifacts VALUES(?,?,?)",("artifacts/"+name+"/file.txt",name+" private sibling body "*200,"SOURCE."+name))
            jid=w._parent(con,s,"A")
            prompt=s["jobs"][jid]["spec"]["prompt"]
            self.assertIn("A private sibling body", prompt)
            bad = []
            def search(node, path=""):
                if isinstance(node, dict):
                    for key, value in node.items():search(value, path+"/"+key)
                elif isinstance(node, list):
                    for index, value in enumerate(node):search(value,path+"/"+str(index))
                elif isinstance(node, str) and "B private sibling body" in node:bad.append(path)
            search(json.loads(prompt))
            self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
