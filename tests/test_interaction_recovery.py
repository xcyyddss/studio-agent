"""Synthetic regressions for publication races, scoped handoffs and host limits."""
from copy import deepcopy
import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_runtime.context_budget import CLI_MAX_INPUT_CHARS, capacity, character_error
from studio_runtime.contracts import JobSpec
from studio_runtime.file_exchange import validate_storage
from studio_runtime.provider import CodexProvider
from studio_runtime.workflow import Workflow
from test_input_lifecycle import empty, output


class InteractionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/".validation")
        self.addCleanup(self.tmp.cleanup)
        self.w = Workflow.create(Path(self.tmp.name)/"run", "Synthetic coordination regression")
        with self.w._tx() as (con,s):
            s["paused"] = True
            for j in s["jobs"].values(): j["state"]="superseded"
            for i in s["inquiries"].values(): i["state"]="closed"
            for name in ("A","B"):
                self.w.parliament._put(con,name,"topic",parent="ROOT",actor=name+".AUTHOR")
                self.w.parliament._put(con,name+".v1","revision",topic=name,parent="ROOT",
                    questions=["Interpret existing diagnostic"],I=[],candidates=[],previous=None,
                    continuity="fresh",unresolved_previous="",relation="complement")
                con.execute("INSERT INTO heads VALUES(?,?)",(name,name+".v1"))
                s["topics"][name]=dict(id=name,parent="ROOT",revision=name+".v1",state="active",
                    summary="",new_returns=[],consumed_returns=[],dirty=False)

    def state(self):
        with self.w._tx() as (_,s): return deepcopy(s)

    def job(self, role="parent",topic="ROOT"):
        with self.w._tx() as (con,s):
            jid = self.w._parent(con,s,topic) if role=="parent" else self.w._author(con,s,topic)
            return deepcopy(s["jobs"][jid]["spec"])

    def apply(self,spec,out):
        result=self.w.apply(spec["job_id"],out,defer_progress=True)
        self.assertEqual(result["status"],"applied",result)
        return result

    def root_question(self):
        spec=self.job()
        out=output(spec,"wait")
        out["questions"]=[dict(text="Complete diagnostic A",why="One baseline",blocking=False,guidance_ids=[],attachments=[])]
        self.apply(spec,out)
        return next(q for q in self.state()["questions"].values() if q["source_job"]==spec["job_id"])

    def proposal(self,spec=None):
        spec=spec or self.job("author","A")
        out=empty(spec["schema"])
        out.update(status="wait",ans="Need evidence",con="Await coordination",
            questions=[dict(text="Complete a second diagnostic",why="No evidence visible locally",blocking=True,guidance_ids=[],attachments=[])])
        self.apply(spec,out)
        return next(q for q in self.state()["questions"].values() if q["source_job"]==spec["job_id"])

    def test_late_diagnostic_staged_and_reused_without_exposing_raw_answer(self):
        q=self.root_question()
        old=self.job("author","A")
        answer=self.w.input("PRIVATE ORIGINAL ANSWER",question_id=q["id"],profile_scope="none")
        proposal=self.proposal(old)
        self.assertEqual(proposal["state"],"proposed")
        with self.assertRaisesRegex(ValueError,"published"):
            self.w.question_presented(proposal["id"])
        with self.assertRaisesRegex(ValueError,"not currently open"):
            self.w.input("should not answer",question_id=proposal["id"])
        parent=self.job()
        task=json.loads(parent["prompt"])["task"]
        self.assertEqual(task["interaction"]["review"][0]["id"],proposal["id"])
        self.assertIn(answer["id"],task["input_obligations"])
        out=output(parent,"wait")
        out["question_decisions"]=[dict(question_id=proposal["id"],action="reuse",
            related_question_id=q["id"],reason="The original diagnostic already has a real answer")]
        out["files"]=[dict(path="report.txt",content="Parent report: one case needs further checking. Source answer retained at parent.")]
        out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",reason="Interpret the actual submitted work",
            summary="Parent analysis of the original answer; no second test",attachments=["@self/report.txt"])]
        self.apply(parent,out)
        s=self.state()
        self.assertEqual(s["questions"][proposal["id"]]["state"],"resolved")
        author=self.job("author","A")
        self.assertNotIn("PRIVATE ORIGINAL ANSWER",author["prompt"])
        self.assertNotIn(answer["id"],json.loads(author["prompt"])["task"]["input_obligations"])
        self.assertIn("Parent analysis of the original answer",author["prompt"])
        fid=next(f["id"] for f in s["file_exchange"].values() if f["origin"]=="model_handoff")
        self.assertEqual(s["file_exchange"][fid]["reads"],[])
        owner_input=next(i for i in s["user_inputs"] if any(f["id"]==fid for f in i.get("attachments",[])))
        self.assertEqual(s["file_exchange"][fid]["input_id"],owner_input["id"])
        self.assertEqual(owner_input["attachments"][0]["input_id"],owner_input["id"])
        # The other child has neither report nor source reply.
        sibling=self.job("author","B")
        self.assertNotIn("Parent analysis of the original answer",sibling["prompt"])
        # A read tool result must be supplied to a successful author call before uptake.
        out=empty(author["schema"]);out.update(status="wait",ans="Read actual report",con="Pending file read")
        out["input_uses"]=[dict(input_id=i,use="Await report reading",effect="Reading scheduled",disposition="followup")
                          for i in json.loads(author["prompt"])["task"]["input_obligations"]]
        out["tool_requests"]=[dict(kind="attachment.read",arguments=json.dumps({"file_id":fid}),purpose="Read handoff report")]
        self.apply(author,out)
        request=next(t for t in self.state()["tool_requests"] if t["source_job"]==author["job_id"])
        value=self.w.attachment_tool(request)
        self.w.tool_result(request["id"],value,defer_progress=True)
        after=self.job("author","A")
        out=empty(after["schema"]);out.update(status="complete",ans="Actual report handled",con="No duplicate diagnostic")
        out["input_uses"]=[dict(input_id=i,use="Use supplied report",effect="No duplicate test",disposition="processed")
                          for i in json.loads(after["prompt"])["task"]["input_obligations"]]
        out["attachment_uses"]=[dict(file_id=fid,decision="adopt",reason="Entire report supplied",effect="Interpret existing work")]
        self.apply(after,out)
        self.assertEqual(self.state()["file_exchange"][fid]["state"],"handled")
        with self.w._tx() as (con,s): validate_storage(con,s)

    def test_publication_readset_race_retains_work_but_requires_fresh_review(self):
        proposal=self.proposal()
        parent=self.job()
        self.w.input("New relevant information",profile_scope="none")
        out=output(parent)
        out["question_decisions"]=[dict(question_id=proposal["id"],action="publish",related_question_id="",reason="New missing evidence")]
        self.apply(parent,out)
        self.assertEqual(self.state()["questions"][proposal["id"]]["state"],"proposed")
        fresh=self.job();out=output(fresh,"wait")
        out["question_decisions"]=[dict(question_id=proposal["id"],action="publish",related_question_id="",reason="Still independently needed after reviewing new input")]
        self.apply(fresh,out)
        self.assertEqual(self.state()["questions"][proposal["id"]]["state"],"open")

    def test_new_root_question_after_input_arrival_is_not_published_stale(self):
        spec=self.job()
        self.w.input("Already completed this diagnostic",profile_scope="none")
        out=output(spec,"wait");out["questions"]=[dict(text="Please diagnose",why="Missing evidence",
            blocking=True,guidance_ids=[],attachments=[])]
        self.apply(spec,out)
        self.assertEqual(next(iter(self.state()["questions"].values()))["state"],"proposed")

    def test_withdrawal_is_structural_and_wakes_owner(self):
        q=self.proposal();p=self.job();out=output(p)
        out["question_decisions"]=[dict(question_id=q["id"],action="withdraw",related_question_id="",reason="Unnecessary repeated work")]
        self.apply(p,out)
        s=self.state()
        self.assertEqual(s["questions"][q["id"]]["state"],"cancelled")
        self.assertTrue(s["topics"]["A"]["author_refresh_required"])
        self.assertTrue(any(x["topic_id"]=="A" and x["kind"]=="coordination" for x in s["user_inputs"]))

    def test_dependency_wait_and_answer_wakeup_without_raw_broadcast(self):
        q=self.root_question();p=self.job();out=output(p)
        out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",summary="",attachments=[],reason="Need this baseline")]
        self.apply(p,out)
        a=self.job("author","A");out=empty(a["schema"]);out.update(status="wait",ans="Wait for handoff",con="No repeated request")
        out["input_uses"]=[dict(input_id=i,use="Wait existing dependency",effect="Avoid duplicate request",disposition="followup")
                          for i in json.loads(a["prompt"])["task"]["input_obligations"]]
        self.apply(a,out)
        self.w.input("private",question_id=q["id"],profile_scope="none")
        s=self.state()
        self.assertEqual(s["question_handoffs"][0]["state"],"awaiting_report")
        self.assertTrue(s["topics"]["ROOT"]["dirty"])
        self.assertFalse(any(x["text"]=="private" and x["topic_id"]=="A" for x in s["user_inputs"]))

    def test_raw_user_file_cannot_be_handed_to_other_topic(self):
        q=self.root_question()
        answer=self.w.input("answer",question_id=q["id"],profile_scope="none",
            attachments=[dict(name="private.txt",data_base64=base64.b64encode(b"private original").decode())])
        fid=answer["attachments"][0]["id"]
        reader=self.job();out=output(reader,"wait")
        out["input_uses"]=[dict(input_id=answer["id"],use="Read original",effect="Prepare analysis",disposition="followup")]
        out["tool_requests"]=[dict(kind="attachment.read",arguments=json.dumps({"file_id":fid}),purpose="Read original")]
        self.apply(reader,out)
        request=next(t for t in self.state()["tool_requests"] if t["source_job"]==reader["job_id"])
        self.w.tool_result(request["id"],self.w.attachment_tool(request),defer_progress=True)
        p=self.job();out=output(p)
        out["attachment_uses"]=[dict(file_id=fid,decision="adopt",reason="Read original",effect="Use it for a report")]
        out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",summary="Unapproved raw transfer",
            attachments=[answer["attachments"][0]["id"]],reason="Try sharing")]
        # The operation is rejected atomically; no new recipient file/input.
        result=self.w.apply(p["job_id"],out,defer_progress=True)
        self.assertEqual(result["status"],"rejected")
        self.assertIn("authored reports",str(self.state()["jobs"][p["job_id"]]["error"]))
        self.assertFalse(any(f["topic_id"]=="A" for f in self.state()["file_exchange"].values()))

    def test_reuse_cannot_strand_received_answer_without_report_or_preparation(self):
        source=self.root_question()
        self.w.input("available answer",question_id=source["id"],profile_scope="none")
        proposed=self.proposal();p=self.job();out=output(p)
        out["question_decisions"]=[dict(question_id=proposed["id"],action="reuse",
            related_question_id=source["id"],reason="Use existing answer")]
        result=self.w.apply(p["job_id"],out,defer_progress=True)
        self.assertEqual(result["status"],"rejected")
        self.assertIn("handoff",str(self.state()["jobs"][p["job_id"]]["error"]))
        self.assertEqual(self.state()["questions"][proposed["id"]]["state"],"proposed")
        self.assertFalse(self.state().get("question_handoffs"))

    def test_multilevel_proposal_escalation_keeps_original_owner(self):
        root=self.root_question()
        with self.w._tx() as (con,s):
            self.w.parliament._put(con,"C","topic",parent="A",actor="C.AUTHOR")
            self.w.parliament._put(con,"C.v1","revision",topic="C",parent="A",questions=["Read baseline"],
                I=[],candidates=[],previous=None,continuity="fresh",unresolved_previous="",relation="complement")
            con.execute("INSERT INTO heads VALUES(?,?)",("C","C.v1"))
            s["topics"]["C"]=dict(id="C",parent="A",revision="C.v1",state="active",summary="",
                new_returns=[],consumed_returns=[],dirty=False)
        child=self.job("author","C")
        out=empty(child["schema"]);out.update(status="wait",ans="Need baseline",con="Question",
            questions=[dict(text="Run baseline",why="Need evidence",blocking=True,guidance_ids=[],attachments=[])])
        self.apply(child,out)
        proposal=next(q for q in self.state()["questions"].values() if q["topic_id"]=="C")
        p=self.job("parent","A");out=output(p)
        out["question_decisions"]=[dict(question_id=proposal["id"],action="escalate",related_question_id="",
            reason="The root owns this baseline and its answer")]
        self.apply(p,out)
        s=self.state();self.assertEqual(s["questions"][proposal["id"]]["reviewer"],"ROOT")
        self.assertTrue(s["topics"]["ROOT"]["dirty"])
        top=self.job();out=output(top)
        out["question_decisions"]=[dict(question_id=proposal["id"],action="reuse",related_question_id=root["id"],
            reason="One original baseline is pending; deliver a scoped report to C later")]
        self.apply(top,out)
        self.assertEqual(self.state()["question_handoffs"][0]["recipient"],"C")
        self.assertEqual(self.state()["questions"][proposal["id"]]["topic_id"],"C")

    def test_withdrawing_source_cancels_pending_dependency_and_survives_restart(self):
        q=self.root_question();p=self.job();out=output(p)
        out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",summary="",attachments=[],reason="Wait baseline")]
        self.apply(p,out);p=self.job();out=output(p)
        out["question_decisions"]=[dict(question_id=q["id"],action="withdraw",related_question_id="",reason="Baseline no longer required")]
        self.apply(p,out)
        self.w=Workflow.open(self.w.path)
        self.assertEqual(self.state()["question_handoffs"][0]["state"],"cancelled")
        self.assertTrue(self.state()["topics"]["A"]["author_refresh_required"])

    def test_new_file_versions_reopen_handoff_without_overwriting_old_reports(self):
        q=self.root_question()
        original=self.w.input("file answer",question_id=q["id"],profile_scope="none",
            attachments=[dict(name="a.txt",data_base64=base64.b64encode(b"v1").decode())])
        fid=original["attachments"][0]["id"]
        with self.w._tx() as (_,s):
            # Synthetic already-delivered predecessor; this test exercises
            # receiving later versions, not fabricated reading evidence.
            s["question_handoffs"]=[dict(id="H",question_id=q["id"],coordinator="ROOT",
                recipient="A",state="delivered",input_id="historical report")]
        latest=self.w.input("revision",topic_id="ROOT",profile_scope="none",
            attachments=[dict(name="a.txt",data_base64=base64.b64encode(b"v2").decode(),replaces=fid)])
        s=self.state()
        self.assertEqual(s["question_handoffs"][0]["state"],"awaiting_report")
        self.assertEqual(s["question_handoffs"][0]["source_version_overrides"][fid],latest["attachments"][0]["id"])
        self.assertEqual(s["question_handoffs"][0]["input_id"],"historical report")
        self.assertEqual(len(s["file_exchange"]),2)

    def test_restricted_question_is_not_disclosed_to_coordination_role(self):
        from studio_runtime.interactions import directory
        with self.w._tx() as (_,s):
            q=dict(id="Q.private",topic_id="A",source_job="OLD",state="open",text="PRIVATE QUESTION CONTENT",
                why="PRIVATE METHOD",blocking=True,audience=["USER","author:A"])
            s["questions"][q["id"]]=q
            self.w._stage_question(s,{"job_id":"OLD","metadata":{"topic_id":"A"},"prompt":json.dumps({"task":{}})},q)
            self.assertEqual(q["state"],"open")
            self.assertEqual(q["publication"]["decision"],"scope_limited_direct")
            self.assertNotIn("PRIVATE QUESTION",json.dumps(directory(s,"ROOT")))
            with self.assertRaisesRegex(ValueError,"audience"):
                self.w._link_question(s,{"job_id":"P","metadata":{"topic_id":"ROOT"}},q,"B","No grant")

    def test_identical_report_does_not_duplicate_recipient_inputs_or_file_versions(self):
        q=self.root_question()
        self.w.input("same evidence",question_id=q["id"],profile_scope="none")
        for _ in range(2):
            p=self.job();out=output(p)
            out["files"]=[dict(path="report.txt",content="Exact same report")]
            out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",summary="Same summary",
                attachments=["@self/report.txt"],reason="Provide report")]
            self.apply(p,out)
        s=self.state()
        self.assertEqual(len([f for f in s["file_exchange"].values() if f["origin"]=="model_handoff"]),1)
        self.assertEqual(len([i for i in s["user_inputs"] if i.get("attachments") and i["topic_id"]=="A"]),1)
        self.assertEqual(len(s["question_handoffs"][0]["duplicate_reports"]),1)

    def test_new_handoff_supersedes_inflight_child_without_losing_late_result(self):
        q=self.root_question()
        old=self.job("author","A")
        p=self.job();out=output(p)
        out["question_handoffs"]=[dict(question_id=q["id"],recipient="A",summary="",attachments=[],reason="Baseline dependency")]
        self.apply(p,out)
        out=empty(old["schema"]);out.update(status="complete",ans="Old local result",con="Before handoff")
        saved=self.w.apply(old["job_id"],out,defer_progress=True)
        self.assertEqual(saved["status"],"late_preserved")
        self.assertEqual(self.state()["jobs"][old["job_id"]]["state"],"superseded")
        with self.w._tx() as (con,s):
            raw=json.loads(con.execute("SELECT body FROM results WHERE result_id=?",(saved["result_id"],)).fetchone()[0])
            self.assertEqual(raw["payload"]["ans"],"Old local result")
            self.assertTrue(s["topics"]["A"]["author_refresh_required"])

    def test_withdrawal_closes_external_escalation_without_inventing_answer(self):
        q=self.root_question()
        with self.w._tx() as (con,s):
            self.w.parliament._put(con,"D.fixture","disposition",action="escalate",target=None)
            s["questions"][q["id"]]["disposition"]="D.fixture"
        p=self.job();out=output(p)
        out["question_decisions"]=[dict(question_id=q["id"],action="withdraw",related_question_id="",reason="Premise no longer needed")]
        self.apply(p,out)
        with self.w._tx() as (con,s):
            resolved=self.w.parliament._all(con,"resolution")
            self.assertEqual(resolved[0]["disposition"],"D.fixture")
            self.assertIn("not a user answer",resolved[0]["note"])
            self.assertNotIn("answer_id",s["questions"][q["id"]])


class CharacterBoundaryTests(unittest.TestCase):
    def test_highly_compressible_text_fits_tokens_but_exceeds_chars(self):
        text="a"*(CLI_MAX_INPUT_CHARS+12020)
        c=capacity(text,{})
        self.assertTrue(c["token_fits"])
        self.assertFalse(c["fits"])
        self.assertEqual(c["remaining_prompt_chars"],-12020)

    def test_saved_stderr_numeric_classification(self):
        diagnostic='Error: Input exceeds the maximum length of 1048576 characters. {"input_error_code":"input_too_large","max_chars":1048576,"actual_chars":1060596} SECRET'
        error=character_error(diagnostic)
        self.assertEqual(error["category"],"context_capacity")
        self.assertEqual(error["actual_chars"],1060596)
        self.assertNotIn("SECRET",json.dumps(error))

    def test_dispatch_rechecks_legacy_job_without_launching(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/".validation") as d:
            job=JobSpec(job_id="OLD",run_id="R",kind="role",role="fixture",schema={},
                prompt="x"*(CLI_MAX_INPUT_CHARS+1),metadata={})
            with patch("studio_runtime.provider.subprocess.run") as launch:
                result=CodexProvider().execute(job,"A",Path(d)/"attempt")
            launch.assert_not_called()
            self.assertEqual(result["error"]["boundary"],"cli_input_chars")
            self.assertEqual(result["outcome"],"failed")
            self.assertFalse(result["receipt"]["fresh_process"])


if __name__=="__main__":
    unittest.main()
