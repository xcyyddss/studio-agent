"""Offline regression tests: synthetic inputs, no model/network or user data.

Run: python -B -m unittest discover -s tests -v
"""
from copy import deepcopy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from studio_runtime.context import project_context, ordinary_input
from studio_runtime.context_lifecycle import ContextPending, capacity, policy
from studio_runtime.contracts import JobSpec
from studio_runtime.input_lifecycle import InputContractError, validate_input_task, validate_input_uses
from studio_runtime.provider import CodexProvider
from studio_runtime.roles import runtime_schema
from studio_runtime.workflow import Workflow, digest


def empty(schema):
    if "enum" in schema:
        return schema["enum"][0]
    return {"object": lambda: {k: empty(v) for k, v in schema["properties"].items()},
            "array": lambda: [], "string": lambda: "", "boolean": lambda: False,
            "integer": lambda: 0, "number": lambda: 0}[schema["type"]]()


def output(spec, action="continue"):
    value = empty(spec["schema"])
    value.update(action=action, summary="Synthetic regression result.", reason="Fixture only.")
    value["input_uses"] = [dict(input_id=i, disposition="processed", use="Handle the supplied fixture",
                               effect="Recorded synthetic processing")
                           for i in json.loads(spec["prompt"])["task"].get("input_obligations", [])]
    if "inquiry_uses" in value:
        value["inquiry_uses"] = [dict(request_id=i["id"], use="Examine actual generation outcome",
            effect="No claim that candidate generation completed an audit")
            for i in json.loads(spec["prompt"])["task"].get("inquiry_results", [])]
    return value


def source_task(count=3):
    inputs = [dict(id=f"INPUT.{i}", kind="tool_result", request_id=f"TOOL.{i}",
                   value={"body": str(i) * 11000}) for i in range(count)]
    return dict(topic="ROOT", goal="Compare complete fixture records", user_inputs=inputs,
                tool_requests=[dict(id=f"TOOL.{i}", state="answered") for i in range(count)],
                input_obligations=[x["id"] for x in inputs],
                prior_tool_result_delivery=[dict(input_id=x["id"], input_sha256=digest(x), job_id="OLD") for x in inputs],
                undisposed_candidates=[], new_returns=[], requests=[], assessments=[], unresolved=[])


class ProjectionTests(unittest.TestCase):
    def test_delivered_unfinished_inputs_stay_required_and_batches_cover_all(self):
        source = source_task()
        bundle = project_context("parent", source, max_chars=42000, schema=runtime_schema("parent", v4=True))
        self.assertGreater(len(bundle["batches"]), 1)
        actual = set()
        for batch in bundle["batches"]:
            task = batch["task"]
            validate_input_task(task, original=source)
            visible = {i["id"] for i in task["user_inputs"]}
            self.assertEqual(set(task["input_obligations"]), visible)
            self.assertEqual(set(task["deferred_input_obligations"]), set(source["input_obligations"]) - visible)
            self.assertLessEqual(len(batch["prompt"]), 42000)
            for item in task["user_inputs"]:
                self.assertEqual(item, next(i for i in source["user_inputs"] if i["id"] == item["id"]))
            actual.update(task["input_obligations"])
        self.assertEqual(actual, set(source["input_obligations"]))

    def test_processed_history_can_be_optional_without_losing_pending_inputs(self):
        source = source_task()
        source["input_obligations"] = ["INPUT.2"]
        bundle = project_context("parent", source, max_chars=42000)
        self.assertEqual(len(bundle["batches"]), 1)
        task = bundle["batches"][0]["task"]
        self.assertEqual(task["input_obligations"], ["INPUT.2"])
        self.assertIn("INPUT.2", {i["id"] for i in task["user_inputs"]})
        self.assertTrue(any(p["reference"]["pointer"].startswith("/user_inputs/") for p in bundle["catalog"]))

    def test_impossible_source_is_rejected_before_projection(self):
        source = source_task(1)
        source["user_inputs"] = []
        with self.assertRaisesRegex(InputContractError, "absent"):
            project_context("parent", source)

    def test_output_cannot_claim_deferred_unknown_or_duplicate_inputs(self):
        source = source_task()
        task = project_context("parent", source, max_chars=42000)["batches"][0]["task"]
        required = task["input_obligations"]
        uses = [dict(input_id=i) for i in required]
        validate_input_uses(task, {"input_uses": uses, "action": "continue"})
        for bad in [task["deferred_input_obligations"][0], "FOREIGN", required[0]]:
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_input_uses(task, {"input_uses": uses + [dict(input_id=bad)]})
        with self.assertRaisesRegex(ValueError, "terminal"):
            validate_input_uses(task, {"input_uses": uses, "action": "quiesce"})

    def test_obligation_erasure_and_duplicate_source_ids_rejected(self):
        source = source_task(1)
        changed = deepcopy(source)
        changed["input_obligations"] = []
        with self.assertRaisesRegex(InputContractError, "lost or invented"):
            validate_input_task(changed, original=source)
        source["user_inputs"].append(deepcopy(source["user_inputs"][0]))
        with self.assertRaisesRegex(InputContractError, "unique"):
            project_context("parent", source)

    def test_unseen_then_oldest_unfinished_delivery_is_prioritized(self):
        source = source_task()
        # An already-delivered old record comes after the one not yet supplied.
        source["prior_tool_result_delivery"] = source["prior_tool_result_delivery"][:2]
        task = project_context("parent", source, max_chars=42000)["batches"][0]["task"]
        self.assertEqual(task["user_inputs"][0]["id"], "INPUT.2")

    def test_invented_deferral_cannot_hide_a_required_body(self):
        source = source_task(1)
        source["user_inputs"] = []
        source["input_obligations"] = []
        source["deferred_input_obligations"] = ["INPUT.0"]
        with self.assertRaisesRegex(InputContractError, "incomplete batch"):
            validate_input_task(source)


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".validation"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.w = Workflow.create(Path(self.temp.name) / "run", "Synthetic input lifecycle regression")
        with self.w._tx() as (_, s):
            for j in s["jobs"].values(): j["state"] = "superseded"
            for i in s["inquiries"].values(): i["state"] = "closed"
            s["topics"]["ROOT"]["dirty"] = False
            s["paused"] = True

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        with self.w._tx() as (_, s): return deepcopy(s)

    def parent(self):
        with self.w._tx() as (con, s): return self.w._parent(con, s, "ROOT")

    def add_results(self, count=3):
        with self.w._tx() as (_, s):
            for item in source_task(count)["user_inputs"]:
                s["user_inputs"].append({**item, "topic_id": "ROOT"})
                s["tool_requests"].append(dict(id=item["request_id"], topic_id="ROOT", state="answered",
                                               kind="artifact.read", source_job="fixture", arguments={}, purpose="fixture"))

    def corrupt_sealed_input(self, jid, iid):
        # Emulate a job saved by an older version. The production seal rejects
        # this packet; only this explicit fault fixture changes its original.
        with self.w._tx() as (_, s):
            spec = s["jobs"][jid]["spec"]
            packet = json.loads(spec["prompt"])
            packet["task"]["user_inputs"] = [x for x in packet["task"]["user_inputs"] if x["id"] != iid]
            spec["prompt"] = json.dumps(packet, ensure_ascii=False)

    def test_historical_failure_rebuilds_even_after_output_retry_exhaustion(self):
        received = self.w.input("A saved answer still requires processing.", profile_scope="none")
        jobs = []
        for _ in range(3):
            jid = self.parent()
            spec = self.state()["jobs"][jid]["spec"]
            good = output(spec, "quiesce")
            self.corrupt_sealed_input(jid, received["id"])
            self.assertEqual(self.w.apply(jid, good)["status"], "rejected")
            with self.w._tx() as (_, s):
                # Older software classified these as model output errors.
                s["jobs"][jid]["failure_kind"] = "contract"
                if jobs:
                    s["jobs"][jid]["spec"]["metadata"]["retry_of"] = jobs[-1]
                    s["jobs"][jobs[-1]]["state"] = "superseded"
            jobs.append(jid)
        old = deepcopy(self.state()["jobs"][jid]["spec"])
        with self.w._tx() as (_, s): s["paused"] = False
        repaired = self.w.repair_contract_failures()
        self.assertEqual(len(repaired), 1)
        new = repaired[0]
        s = self.state()
        self.assertEqual(s["jobs"][jid]["spec"], old)
        self.assertEqual(s["jobs"][new]["spec"]["metadata"]["repair_kind"], "input_contract")
        task = json.loads(s["jobs"][new]["spec"]["prompt"])["task"]
        validate_input_task(task)
        self.assertIn("input_rebuild", task)
        self.assertNotIn("validation_failure", task)
        self.assertEqual(task["input_obligations"], [received["id"]])
        with self.w._tx() as (_, state): state["paused"] = True
        result = self.w.apply(new, output(s["jobs"][new]["spec"], "quiesce"))
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.state()["input_receipts"][received["id"]]["state"], "processed")
        with closing(sqlite3.connect(self.w.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM results WHERE outcome='rejected'").fetchone()[0], 3)

    def test_parent_batches_make_progress_without_bypassing_receipts(self):
        self.add_results(6)
        # Isolate batch transport from asynchronous semantic compression.
        with self.w._tx() as (_, s):
            s["metadata"]["context_policy"] = {"model_context_tokens":40000, "output_reserve_tokens":2000, "platform_reserve_tokens":2000}
        handled = set()
        for _ in range(6):
            jid = self.parent()
            spec = self.state()["jobs"][jid]["spec"]
            task = json.loads(spec["prompt"])["task"]
            validate_input_task(task)
            self.assertTrue(capacity(spec["prompt"], spec["schema"], settings=policy(self.state()))["fits"])
            self.assertTrue(set(task["input_obligations"]) - handled)
            out = output(spec, "continue" if task["deferred_input_obligations"] else "quiesce")
            self.assertEqual(self.w.apply(jid, out)["status"], "applied")
            handled.update(task["input_obligations"])
            if not task["deferred_input_obligations"]: break
        self.assertEqual(handled, {f"INPUT.{i}" for i in range(6)})
        self.assertTrue(all(x["state"] == "processed" for x in self.state()["input_receipts"].values()))

    def test_followup_is_mandatory_after_successful_delivery(self):
        self.add_results(1)
        jid = self.parent()
        out = output(self.state()["jobs"][jid]["spec"])
        out["input_uses"][0]["disposition"] = "followup"
        out["unresolved"] = [dict(text="Need an additional observation", condition="Observation available", next_action="Compare it")]
        self.assertEqual(self.w.apply(jid, out)["status"], "applied")
        nxt = self.parent()
        task = json.loads(self.state()["jobs"][nxt]["spec"]["prompt"])["task"]
        self.assertEqual(task["input_obligations"], ["INPUT.0"])
        self.assertIn("INPUT.0", {x["id"] for x in task["user_inputs"]})

    def test_followup_wait_uses_existing_real_dependency_without_duplicate_request(self):
        answer = self.w.input("Check a fact before using my answer", profile_scope="none")
        with self.w._tx() as (_, s):
            s["tool_requests"].append(dict(id="WAITING", topic_id="ROOT", state="requested", source_job="fixture",
                                           kind="artifact.read", arguments={}, purpose="fixture dependency"))
        jid = self.parent()
        out = output(self.state()["jobs"][jid]["spec"], "wait")
        out["input_uses"][0]["disposition"] = "followup"
        self.assertEqual(self.w.apply(jid, out)["status"], "applied")
        s = self.state()
        self.assertEqual(len(s["tool_requests"]), 1)
        self.assertEqual(s["input_receipts"][answer["id"]]["state"], "followup")

    def test_followup_without_real_next_step_still_fails(self):
        self.w.input("Synthetic unanswered duty", profile_scope="none")
        jid = self.parent(); out = output(self.state()["jobs"][jid]["spec"], "wait")
        out["input_uses"][0]["disposition"] = "followup"
        self.assertEqual(self.w.apply(jid, out)["status"], "rejected")
        self.assertIn("executable follow-up", self.state()["jobs"][jid]["error"])

    def test_real_dependency_resolving_during_call_keeps_followup_runnable(self):
        self.w.input("Need this observation", profile_scope="none")
        with self.w._tx() as (_, s):
            s["tool_requests"].append(dict(id="PENDING", topic_id="ROOT", state="requested", source_job="fixture",
                                           kind="artifact.read", arguments={}, purpose="fixture"))
        jid = self.parent(); out = output(self.state()["jobs"][jid]["spec"], "wait")
        out["input_uses"][0]["disposition"] = "followup"
        self.w.tool_result("PENDING", dict(outcome="succeeded", payload={"content":"arrived"}), source="fixture")
        self.assertEqual(self.w.apply(jid, out)["status"], "applied")
        self.assertTrue(self.state()["topics"]["ROOT"]["dirty"])

    def test_verified_history_compaction_preserves_unfinished_duties_and_source(self):
        with self.w._tx() as (_, s):
            s["metadata"]["context_policy"] = {"model_context_tokens":40000, "output_reserve_tokens":2000,
                "platform_reserve_tokens":2000, "source_chunk_chars":8000, "summary_target_chars":2000}
            for i in range(4):
                iid = f"OLD.{i}"
                s["user_inputs"].append(dict(id=iid, topic_id="ROOT", kind="input", text="Limit 17. " * 900))
                s["input_receipts"][iid] = {"state":"processed"}
            s["user_inputs"].append(dict(id="OPEN", topic_id="ROOT", kind="input", text="Unresolved alternative"))
            s["input_receipts"]["OPEN"] = {"state":"followup"}
        for _ in range(30):
            with self.w._tx() as (con, s):
                try:
                    task = self.w._topic_task(con, s, "ROOT")
                    prompt, _ = self.w._project(s, "parent", task, runtime_schema("parent", v4=True))
                except ContextPending:
                    prompt = None
            if prompt:
                break
            for jid, job in self.state()["jobs"].items():
                if job["state"] != "pending" or job["spec"]["role"] not in {"context_summary", "context_review"}:
                    continue
                spec = job["spec"]; task = json.loads(spec["prompt"])["task"]
                if spec["role"] == "context_review":
                    out = dict(accepted=True, missing=[], distorted=[], unsupported=[], missing_constraints=[], reason="Synthetic coverage fixture")
                else:
                    src = task["sources"][0]
                    out = dict(summary="The processed historical inputs require limit 17.", constraints=["Limit 17"],
                        decisions=[], conflicts=[], open_questions=[], early_directions=[], exact_quotes=[],
                        coverage=[{k:src[k] for k in ("source_id", "start", "end")} | dict(treatment="consolidated", reason="Repeated fixture constraint")])
                self.assertEqual(self.w.apply(jid, out)["status"], "applied")
        self.assertIsNotNone(prompt)
        task = json.loads(prompt)["task"]
        self.assertIn("history_memory", task)
        self.assertEqual(task["input_obligations"], ["OPEN"])
        self.assertEqual([x["id"] for x in task["user_inputs"]], ["OPEN"])
        self.assertEqual(len(self.state()["user_inputs"]), 5)
        validate_input_task(task)

    def test_preparation_cannot_silently_drop_pending_input(self):
        self.w.input("Must survive preparation", profile_scope="none")
        with self.w._tx() as (_, s): s["topics"]["ROOT"]["context_pressure"] = {"job_id":"forced", "target_text_tokens":1000}
        def broken(state, role, task, **kw):
            return {**task, "user_inputs":[]}
        with patch.object(self.w, "_prepare_context", side_effect=broken):
            with self.assertRaisesRegex(InputContractError, "changed outstanding"):
                self.parent()

    def test_author_retry_rebuilds_current_input_instead_of_replaying_impossible_packet(self):
        received = self.w.input("Synthetic pending input", profile_scope="none")
        with self.w._tx() as (con, s): jid = self.w._author(con, s, "ROOT")
        spec = self.state()["jobs"][jid]["spec"]
        self.corrupt_sealed_input(jid, received["id"])
        self.assertEqual(self.w.apply(jid, empty(spec["schema"]))["status"], "rejected")
        new = self.w.retry(jid)
        s = self.state(); task = json.loads(s["jobs"][new]["spec"]["prompt"])["task"]
        self.assertEqual(s["jobs"][new]["spec"]["role"], "author")
        self.assertEqual(task["input_obligations"], [received["id"]])
        validate_input_task(task)
        self.assertFalse(s["topics"]["ROOT"]["author_refresh_required"])

    def test_author_output_repair_includes_late_input_but_not_other_topic(self):
        first = self.w.input("First input", profile_scope="none")
        with self.w._tx() as (con, s): jid = self.w._author(con, s, "ROOT")
        spec = self.state()["jobs"][jid]["spec"]
        self.assertEqual(self.w.apply(jid, empty(spec["schema"]))["status"], "rejected")
        second = self.w.input("Arrived after the failed call was sealed", profile_scope="none")
        with self.w._tx() as (_, s):
            s["user_inputs"].append(dict(id="PRIVATE", topic_id="USER", kind="input", text="Private user topic material"))
        new = self.w.retry(jid)
        task = json.loads(self.state()["jobs"][new]["spec"]["prompt"])["task"]
        self.assertEqual(set(task["input_obligations"]), {first["id"], second["id"]})
        self.assertNotIn("PRIVATE", {i["id"] for i in task["user_inputs"]})
        self.assertIn("validation_failure", task)

    def test_output_repair_remains_bounded_and_transport_retries_do_not_spend_it(self):
        self.w.input("An input requiring acknowledgment", profile_scope="none")
        jid = self.parent()
        for n in range(2):
            self.w.apply(dict(result_id=f"NETWORK.{n}", job_id=jid, outcome="failed", payload=None,
                              error={"category":"transport", "message":"Fixture network failure"}))
            jid = self.w.retry(jid)
        with self.w._tx() as (_, s): s["paused"] = False
        for n in range(3):
            spec = self.state()["jobs"][jid]["spec"]
            bad = output(spec); bad["input_uses"] = []
            self.assertEqual(self.w.apply(jid, bad)["status"], "rejected")
            replacements = self.w.repair_contract_failures()
            if n < 2:
                self.assertEqual(len(replacements), 1)
                jid = replacements[0]
            else:
                self.assertEqual(replacements, [])
                self.assertEqual(self.state()["diagnostics"][-1]["kind"], "contract_repair_exhausted")

    def test_schedule_and_provider_preflight_do_not_blame_model_or_launch_cli(self):
        source = source_task(1); source["user_inputs"] = []
        prompt = ordinary_input("parent", source, schema=runtime_schema("parent", v4=True))
        with self.w._tx() as (con, s):
            prior = set(s["jobs"])
            ok = self.w._budgeted_schedule(con, s, "fixture-invalid-packet",
                lambda: self.w._job(s, "parent", "ROOT", prompt))
            self.assertFalse(ok)
            self.assertEqual(set(s["jobs"]), prior)
            self.assertEqual(s["diagnostics"][-1]["kind"], "input_contract_blocked")
        job = JobSpec(job_id="legacy-bad", run_id="test", kind="parent", role="parent", prompt=prompt,
                      schema=runtime_schema("parent", v4=True), metadata={})
        with patch("studio_runtime.provider.subprocess.run") as launch:
            result = CodexProvider().execute(job, "preflight", Path(self.temp.name) / "attempt")
        launch.assert_not_called()
        self.assertEqual(result["error"]["category"], "input_contract")
        self.assertIsNone(result["receipt"]["pid"])
        self.assertFalse(result["receipt"]["fresh_process"])

    def test_failed_ack_import_rolls_back_all_receipts_and_artifacts(self):
        self.w.input("Synthetic input", profile_scope="none")
        jid = self.parent(); spec = self.state()["jobs"][jid]["spec"]
        out = output(spec)
        out["input_uses"].append(deepcopy(out["input_uses"][0]))
        out["files"] = [dict(path="must-not-exist.txt", content="uncommitted")]
        self.assertEqual(self.w.apply(jid, out)["status"], "rejected")
        self.assertEqual(self.state()["input_receipts"], {})
        with closing(sqlite3.connect(self.w.db)) as con:
            self.assertEqual(con.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
