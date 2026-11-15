"""State size, dependency closure, evidence delivery, and isolated recovery."""
from copy import deepcopy
from contextlib import closing
import json
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from studio_runtime.workflow import Workflow
from studio_runtime.state_store import StateStore
from studio_runtime import user_collaboration as user
from studio_runtime.work_state import working_history
from test_input_lifecycle import empty, output


class LongRunningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.validation')
        self.addCleanup(self.tmp.cleanup)
        self.w = Workflow.create(Path(self.tmp.name)/'run', 'Synthetic dependency and history acceptance')
        with self.w._tx() as (c,s):
            s['paused']=True
            for j in s['jobs'].values(): j['state']='superseded'
            for r in s['inquiries'].values(): r['state']='closed'
            self.w.parliament._put(c,'A','topic',parent='ROOT',actor='A.AUTHOR')
            self.w.parliament._put(c,'A.v1','revision',topic='A',parent='ROOT',questions=['Process exact parent-owned material'],
                I=[],candidates=[],previous=None,continuity='fresh',unresolved_previous='',relation='complement')
            c.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
            s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False)

    def state(self):
        with self.w._read_tx() as (_,s): return deepcopy(s)

    def job(self, role='author', topic='A'):
        with self.w._tx() as (c,s):
            jid = self.w._author(c,s,topic) if role=='author' else self.w._parent(c,s,topic)
            return deepcopy(s['jobs'][jid]['spec'])

    def author(self, spec, status='checkpoint'):
        value=empty(spec['schema'])
        value.update(status=status,ans='Available stage result',con='Exact parent material still needed',
            progress={'kind':'operational','reason':'Dependency registration, not a new cognitive contribution'})
        value['input_uses']=[dict(input_id=i,use='Read supplied input',effect='Changed current result',disposition='processed')
                             for i in json.loads(spec['prompt'])['task']['input_obligations']]
        return value

    def apply(self, spec, value):
        result=self.w.apply(spec['job_id'],value,defer_progress=True)
        self.assertEqual(result['status'],'applied',self.state()['jobs'][spec['job_id']].get('error'))

    def request(self):
        spec=self.job(); value=self.author(spec)
        value['internal_requests']=[dict(key='contract-v1',kind='material',need='Exact contract and boundaries')]
        self.apply(spec,value)
        return self.state()['internal_requests'][0]

    def test_new_internal_dependency_partial_delivery_and_eight_event_cycles(self):
        for n in range(8):
            spec=self.job(); value=self.author(spec)
            value['internal_requests']=[dict(key=f'contract-{n}',kind='material',need=f'Exact contract version {n}')]
            self.apply(spec,value)
            req=self.state()['internal_requests'][-1]
            self.assertEqual(self.state()['topics']['A']['state'],'waiting')
            parent=self.job('parent','ROOT'); p=output(parent,'wait')
            p['internal_responses']=[dict(request_id=req['id'],outcome='deliver',report='Actual source provided',
                artifacts=['@self/contract.txt'],wait_for=[])]
            p['files']=[dict(path='contract.txt',content=f'exact contract {n}: fixed=7; unknown=not provided')]
            self.apply(parent,p)
            self.w.resume(); jobs=self.w.advance()
            child=next(j for j in jobs if j.role=='author' and j.metadata['topic_id']=='A')
            self.assertIn(f'exact contract {n}',child.prompt)
            val=self.author(child.to_dict(),'wait')
            val['questions']=[dict(text=f'Provide independent event {n}',why='External requirement',blocking=True,guidance_ids=[],attachments=[])]
            self.apply(child.to_dict(),val)
            # Close the fixture-generated question administratively for this
            # cycle; use a genuine registered tool dependency for stable idle.
            with self.w._tx() as (_,s):
                for q in s['questions'].values(): q['state']='open';q.pop('reviewer',None)
                s['topics']['ROOT']['dirty']=False
            count=len(self.state()['jobs'])
            self.w=Workflow(self.w.path)
            for _ in range(12): self.assertEqual(self.w.advance(),[])
            self.assertEqual(count,len(self.state()['jobs']))
            self.assertEqual(self.state()['internal_requests'][-1]['state'],'consumed')
            with self.w._tx() as (_,s):
                s['paused']=True
                for q in s['questions'].values(): q['state']='cancelled'

    def test_defer_requires_real_condition_then_wakes_owner_once(self):
        req=self.request(); p=self.job('parent','ROOT'); val=output(p,'wait')
        val['questions']=[dict(text='Supply exact contract',why='Original is absent',blocking=True,guidance_ids=[],attachments=[])]
        # Defer accepts self aliases after actual registration, not guessed IDs.
        val['internal_responses']=[dict(request_id=req['id'],outcome='defer',report='Awaiting source',artifacts=[],wait_for=['@self/question/0'])]
        self.apply(p,val)
        q=next(q for q in self.state()['questions'].values() if q['source_job']==p['job_id'])
        self.w.resume()
        for _ in range(10): self.assertEqual(self.w.advance(),[])
        self.w.input('Actual supplied contract',question_id=q['id'],source='fixture',profile_scope='none')
        jobs=self.w.advance()
        self.assertEqual(len([j for j in jobs if j.role=='parent']),1)
        self.assertEqual(self.state()['internal_requests'][0]['state'],'open')

    def test_internal_response_does_not_discard_inflight_wait_and_uses_file_byte_hash(self):
        req=self.request(); old=self.job()
        p=self.job('parent','ROOT'); val=output(p,'wait')
        text='精确原件\nflag=true\n'
        val['files']=[dict(path='source.txt',content=text)]
        val['internal_responses']=[dict(request_id=req['id'],outcome='deliver',report='Exact version',artifacts=['@self/source.txt'],wait_for=[])]
        self.apply(p,val)
        self.assertEqual(self.state()['jobs'][old['job_id']]['state'],'pending')
        self.apply(old,self.author(old,'wait'))
        s=self.state(); actual=s['user_inputs'][-1]
        self.assertEqual(actual['artifacts'][0]['sha256'],hashlib.sha256(text.encode('utf-8')).hexdigest())
        self.assertTrue(s['topics']['A']['author_refresh_required'])
        self.w.resume(); jobs=self.w.advance()
        child=[j for j in jobs if j.role=='author' and j.metadata['topic_id']=='A']
        self.assertEqual(len(child),1)
        self.assertIn(actual['id'],json.loads(child[0].prompt)['task']['input_obligations'])

    def test_effective_history_reads_index_not_large_original_results(self):
        self.request(); spec=self.job(); val=self.author(spec,'wait')
        val['files']=[dict(path='large.txt',content='data\n'*100000)]
        self.apply(spec,val)
        with self.w._tx() as (c,s):
            def deny_results(action, table, *args):
                return sqlite3.SQLITE_DENY if action==sqlite3.SQLITE_READ and table=='results' else sqlite3.SQLITE_OK
            c.set_authorizer(deny_results)
            history=self.w._work_history(c,s,'A','author')
            self.assertIn('Available stage result',json.dumps(history))
            self.assertLess(len(json.dumps(history)),20000)
            c.set_authorizer(None)
        with self.w._tx() as (c,s):
            full=self.w._work_history(c,s,'A','author',archived=True)
            self.assertEqual(full[-1]['output']['files'][0]['content'],'data\n'*100000)

    def test_historical_file_locator_binds_exact_body_directly_to_review(self):
        self.request(); first=self.job(); val=self.author(first,'wait')
        val['files']=[dict(path='old.txt',content='EXACT constraint=17\n'+'filler '*3000)]
        self.apply(first,val)
        second=self.job(); self.apply(second,self.author(second,'wait'))
        spec=self.job(); task=json.loads(spec['prompt'])['task']
        with self.w._read_tx() as (_,s):
            original=s['context_snapshots'][spec['metadata']['context_projection']['source_sha256']]
            loc=original['public_evidence']['artifacts'][0]
            self.assertFalse(loc['body_delivered']);self.assertNotIn('content',loc)
        path='artifacts/'+first['job_id']+'/old.txt'
        args=dict(operation='bounded_review',brief=dict(situation='Check exact file',observations='',interpretation='',global_question='Validate scope'),
            sources=[dict(id=path,pointer='/public_evidence/artifacts/0/content',window='B')],constraints=[],withheld_sources=[],
            checklist=[dict(id='C1',question='What is the exact constraint?',source_ids=[path])])
        val=self.author(spec,'wait');val['tool_requests']=[dict(kind='cognition.process',arguments=json.dumps(args),purpose='Finite check of exact version')]
        self.apply(spec,val)
        request=self.state()['tool_requests'][-1]
        self.assertIsNone(self.w.cognitive_result(request))
        s=self.state(); plan=s['cognitive_operations'][request['id']]
        source=plan['sources'][0]
        self.assertEqual(source['content'],'EXACT constraint=17\n'+'filler '*3000)
        self.assertTrue(source['source']['resolved_artifact']['complete'])
        self.assertEqual(source['source']['permission_pointer'],'/public_evidence/artifacts/0')
        self.assertIn('EXACT constraint=17',s['jobs'][plan['active_job']]['spec']['prompt'])
        self.assertEqual(s['jobs'][plan['active_job']]['spec']['role'],'processing')
        with self.w._read_tx() as (c,s):
            material=self.w._sources(c,s,references=[request['id']],topic='A')
            self.assertTrue(material)
            self.assertFalse(any(m.get('denial_reason') for m in material),material)

    def test_ui_internal_receipt_does_not_reload_delivered_body(self):
        with self.w._tx() as (_,s):
            s['user_inputs'].append(dict(id='I.BIG',kind='internal_response',topic_id='A',text='x'*100000))
            s.setdefault('internal_requests',[]).append(dict(id='D.BIG',input_id='I.BIG',state='consumed',
                response=dict(report='Exact file delivered',artifacts=['artifacts/J/large.txt'])))
        with patch.object(StateStore,'fetch',side_effect=AssertionError('UI receipt loaded archive')):
            with self.w._read_tx() as (_,s):view=self.w._input_status(s,compact=True)
        self.assertIn('large.txt',view[0]['text'])
        self.assertLess(len(view[0]['text']),100)

    def test_no_change_cannot_hide_phase_transition_or_stage_publication(self):
        from studio_runtime.work_history import validate_progress
        value={'action':'wait','progress':{'kind':'no_change','reason':'Keep the same dependency'}}
        validate_progress(value)
        for mutation in ({'phase':{'name':'New phase'}},{'stage_artifacts':['artifacts/J/result.txt']}):
            with self.assertRaisesRegex(ValueError,'new work/decisions'):
                validate_progress({**value,**mutation})

    def test_checkpoint_roundtrip_keeps_archived_state_and_work_index(self):
        from studio_runtime.checkpoints import export_snapshot, verify_snapshot, restore_snapshot, fork_snapshot
        from studio_runtime.service import StudioService
        from studio_runtime.queue import QueueStore
        base=Path(self.tmp.name)/'checkpoints'
        service=StudioService(base);self.addCleanup(service.close)
        service.create('Synthetic archive checkpoint',run_id='source',web_search='disabled')
        w=Workflow(base/'source');QueueStore(w.path/'queue.sqlite')
        with w._tx() as (c,s):
            for job in s['jobs'].values():job['state']='superseded'
            for inquiry in s['inquiries'].values():inquiry['state']='closed'
            for topic in s['topics'].values():topic.update(state='quiescent',dirty=False)
            s.update(state='quiescent',state_reason='Fixture terminal snapshot')
            s['context_snapshots']['fixture.archive']={'body':'archived original '*5000}
            c.execute('INSERT INTO work_reports VALUES(?,?,?,?)',('FIXTURE.REPORT','ROOT','parent',json.dumps({'meaning':'retained semantic result'})))
        from studio_runtime.service import run_lock
        with run_lock(w.path/'supervisor.lock'):
            export_snapshot(w.path,base/'snapshot',lock_held=True)
        self.assertTrue(verify_snapshot(base/'snapshot')['valid'])
        inspection=restore_snapshot(base/'snapshot',base/'inspection')
        self.assertTrue(inspection['checkpoint_read_only'])
        fork_snapshot(base/'snapshot',base/'fork',run_id='fork')
        with Workflow(base/'fork')._read_tx() as (c,s):
            self.assertEqual(s['run_id'],'fork')
            self.assertEqual(s['context_snapshots']['fixture.archive']['body'],'archived original '*5000)
            self.assertEqual(json.loads(c.execute('SELECT body FROM work_reports WHERE job_id=?',('FIXTURE.REPORT',)).fetchone()[0])['meaning'],'retained semantic result')
        self.assertTrue(verify_snapshot(base/'snapshot')['valid'])

    def test_same_owner_contributions_share_one_call_but_keep_separate_verdicts(self):
        req=self.request(); spec=self.job(); val=self.author(spec)
        val['progress']={'kind':'advance','reason':'Three different findings'}
        val['contributions']=[dict(text=f'Finding {i} with its boundary',candidate_ids=[]) for i in range(3)]
        self.apply(spec,val)
        with self.w._tx() as (c,s):self.w._drain_schedule_intents(c,s)
        jobs=[j['spec'] for j in self.state()['jobs'].values() if j['spec']['role']=='assessment']
        self.assertEqual(len(jobs),1)
        job=jobs[0];self.assertTrue(job['metadata']['batched_assessment'])
        value={'assessments':[dict(contribution_index=i,judgment='uncertain',reason=f'Independent verdict {i}',
            abstract=f'Conditional lesson {i}',scenario=f'Observed scenario {i}',conditions='Fixture only',promote=False) for i in range(3)]}
        bad=deepcopy(value);bad['assessments'][2]['contribution_index']=1
        self.assertEqual(self.w.apply(job['job_id'],bad,defer_progress=True)['status'],'rejected')
        with self.w._tx() as (_,s):s['jobs'][job['job_id']]['state']='pending'
        self.apply(job,value)
        with self.w._read_tx() as (c,s):
            assessments=self.w.parliament._all(c,'assessment')
            self.assertEqual(sorted(a['contribution_index'] for a in assessments),[0,1,2])
            self.assertEqual(len(self.w.parliament._all(c,'learning')),3)
        with self.w._tx() as (c,s):
            self.w._schedule_assessments(c,s,s['jobs'][spec['job_id']]['spec'],s['topics']['A']['checkpoint']['return_id'],val)
            self.assertEqual(len([j for j in s['jobs'].values() if j['spec']['role']=='assessment']),1)
        repair=self.w.repair_context_delivery([self.state()['topics']['A']['checkpoint']['return_id']],
            reason='Explicit operator requests fresh source-bearing reassessment',source='fixture')
        self.assertEqual(len(repair['jobs']),1)
        self.assertNotEqual(repair['jobs'][0],job['job_id'])
        with self.assertRaisesRegex(ValueError,'already has a pending'):
            self.w.repair_context_delivery([self.state()['topics']['A']['checkpoint']['return_id']],reason='Duplicate',source='fixture')

    def test_status_before_execution_queue_exists_is_read_only(self):
        from studio_runtime.queue import QueueStore
        path=Path(self.tmp.name)/'missing.sqlite'
        queue=QueueStore(path,read_only=True)
        self.assertEqual(queue.status()['active'],0)
        self.assertEqual(queue.jobs(include_spec=False),[])
        self.assertFalse(path.exists())

    def test_source_json_cannot_be_mistaken_for_storage_pointer(self):
        source={'nested':{'$studio_body_v1':'not-a-stored-hash'},'$studio_literal_v1':'an ordinary source key'}
        with self.w._tx() as (_,s):s['literal_source']=[source,{'$studio_literal_v1':{'x':'long'*2000}}]
        with self.w._read_tx() as (_,s):
            self.assertEqual(deepcopy(s['literal_source']),[source,{'$studio_literal_v1':{'x':'long'*2000}}])

    def test_selection_delivery_records_actual_recipient_and_parent_uptake(self):
        with self.w._tx() as (c,s):
            jid=next(iter(s['jobs']));s['jobs'][jid]['state']='pending'
            s['inquiries']['REQ.TEST']=dict(id='REQ.TEST',state='generating',topic_id='ROOT',jobs=[jid],candidates=[])
            s['obligations'].append(dict(id='SELECT.TEST',kind='selection',state='delivery_prepared',topic_id='ROOT',
                delivery_receipts=[dict(request_id='REQ.TEST',member_id='M.COGNITION',job_ids=[jid])]))
            s['evidence_deliveries'].append(dict(request_id='REQ.TEST',member_id='M.COGNITION',responding_to=['SELECT.TEST'],
                state='sealed_for_dispatch',batches=[dict(job_id=jid,material_ids=[])]))
            self.w._progress(c,s);self.assertEqual(s['obligations'][-1]['state'],'delivery_prepared')
            s['jobs'][jid].update(state='applied',result_id='REAL.RESULT.MARKER')
            self.w._progress(c,s);self.assertEqual(s['obligations'][-1]['state'],'consumer_processed')
            s['inquiries']['REQ.TEST']['uptake']=[dict(job_id='PARENT.RESULT',use='Read actual output',effect='Recorded conditional result')]
            self.w._progress(c,s);self.assertEqual(s['obligations'][-1]['state'],'delivery_acknowledged')

    def test_incomplete_assessment_schedule_cannot_publish_partial_group_as_complete(self):
        self.request(); spec=self.job(); val=self.author(spec)
        val['progress']={'kind':'advance','reason':'New finding'}
        val['contributions']=[dict(text='Partial assessment preparation fixture',candidate_ids=[])]
        self.apply(spec,val)
        with self.w._tx() as (c,s):
            intent=next(i for i in s['schedule_intents'] if i['kind']=='assessments')
            ret=intent['arguments']['returned']
            self.w._run_schedule_intent(c,s,intent['id'])
            job=next(j for j in s['jobs'].values() if j['spec']['role']=='assessment')
            job.update(state='applied',result_id='FIXTURE.PARTIAL')
            intent['state']='pending';s['paused']=False
            with patch.object(self.w,'_drain_schedule_intents',return_value=True),patch.object(self.w,'_memory_review'),patch.object(self.w,'_user_review'):
                self.w._progress(c,s)
            self.assertNotIn(ret,s['topics']['ROOT']['new_returns'])
            intent['state']='scheduled'
            with patch.object(self.w,'_memory_review'),patch.object(self.w,'_user_review'):
                self.w._progress(c,s)
            self.assertIn(ret,s['topics']['ROOT']['new_returns'])

    def test_unknown_wait_cannot_be_invented_and_no_parent_scope_bypass(self):
        p=self.job('parent','ROOT'); val=output(p,'wait')
        val['internal_requests']=[dict(key='bad',kind='decision',need='ROOT has no parent')]
        self.assertEqual(self.w.apply(p['job_id'],val,defer_progress=True)['status'],'rejected')
        self.assertEqual(self.state().get('internal_requests',[]),[])

    def test_archive_storage_is_lazy_atomic_and_status_does_not_write(self):
        with self.w._tx() as (_,s):
            for n in range(200):
                s['context_snapshots'][str(n)]={'body':str(n)+('x'*100000)}
        with closing(sqlite3.connect(self.w.db)) as con:
            before=con.execute("SELECT body FROM workflow WHERE key='state'").fetchone()[0]
            self.assertLess(len(before),180000)
            self.assertGreater(con.execute('SELECT sum(length(body)) FROM state_bodies').fetchone()[0],20000000)
        with patch.object(StateStore,'fetch',side_effect=AssertionError('status loaded archive')):
            self.w.status()
        with closing(sqlite3.connect(self.w.db)) as con:
            self.assertEqual(before,con.execute("SELECT body FROM workflow WHERE key='state'").fetchone()[0])
        with self.assertRaises(RuntimeError):
            with self.w._tx() as (_,s):
                s['context_snapshots']['rolled-back']={'body':'new'*20000}
                raise RuntimeError('simulate failure')
        with self.w._read_tx() as (_,s):
            self.assertNotIn('rolled-back',s['context_snapshots'])
            self.assertEqual(s['context_snapshots']['199']['body'],'199'+'x'*100000)

    def test_effective_knowledge_versions_and_conditional_closure_survive_restart(self):
        req=self.request()
        a=self.job(); val=self.author(a,'wait')
        val['progress']={'kind':'advance','reason':'New conditional insight'}
        val['work_updates']=[dict(item_id='',kind='hypothesis',statement='Two explanations remain',condition='Await contract',status='disputed',reason='Evidence incomplete')]
        val['unresolved']=[dict(text='Check contract',condition='On material arrival',next_action='Compare explanations')]
        self.apply(a,val)
        a=self.job(); task=json.loads(a['prompt'])['task']; item=task['working_state']['items'][0]
        val=self.author(a,'wait'); val['progress']={'kind':'advance','reason':'Explicit correction'}
        val['work_updates']=[dict(item_id=item['id'],kind='hypothesis',statement='Retain alternative only',condition='New evidence still required',status='conditional',reason='One interpretation withdrawn')]
        val['obligation_decisions']=[dict(id=task['unresolved'][0]['id'],state='withdrawn',reason='This check was redundant; request remains separately tracked')]
        self.apply(a,val)
        self.w=Workflow(self.w.path); s=self.state()
        self.assertEqual(len(s['knowledge_versions']),2)
        self.assertEqual(s['obligations'][0]['state'],'withdrawn')
        self.assertEqual(s['working_knowledge']['A'][item['id']]['status'],'conditional')

    def test_user_invalid_asset_is_quarantined_without_poisoning_valid_observation(self):
        with self.w._tx() as (_,s):
            user.record_event(s,'U1','observation',source='human',content='I prefer concrete examples',allowed_uses=['profile','collaboration'],audience=['*'])
            ctx=user.context_for_review(s,max_chars=100000)
            asset=dict(id='GOOD',kind='profile_claim',content='Explicit preference for examples',conditions='This task',basis='human_observation',
                evidence_event_ids=['U1'],related_asset_ids=[],revision_of='',conflict_with=[],allowed_uses=['profile','collaboration'],audience=['*'],topic_ids=['ROOT'],status='supported')
            bad={**asset,'id':'BAD','kind':'guidance_method','basis':'model_inference'}
            report=dict(processed_event_ids=ctx['pending_event_ids'],assets=[asset,bad],summary='Observed preference; no proof of method benefit',unknowns=[],wake_conditions=[])
            outcome=user.apply_review(s,report,job_id='USERTEST',context=ctx,isolate_invalid=True)
            self.assertEqual(outcome['quarantined_count'],1)
            self.assertIn('GOOD',s['user_collaboration']['assets']);self.assertNotIn('BAD',s['user_collaboration']['assets'])
            user.record_event(s,'U2','observation',source='human',content='New independent observation')
            self.assertEqual([e['id'] for e in user.pending_events(s)],['U2'])

    def test_history_keeps_semantics_without_reembedding_code(self):
        source=[dict(job_id='J1',result_id='R1',role='author',revision='A.v1',created_at='t',output={
            'ans':'A useful conditional finding','con':'Only under X','contributions':[{'text':'Keep both alternatives','candidate_ids':[]}],
            'files':[{'path':'large.py','content':'line\n'*100000}], 'tool_requests':[{'kind':'artifact.read','arguments':'{}'}]})]
        projected=working_history(source)
        self.assertLess(len(json.dumps(projected)),2000)
        self.assertEqual(projected[0]['output']['contributions'],source[0]['output']['contributions'])
        self.assertEqual(source[0]['output']['files'][0]['content'],'line\n'*100000)


if __name__=='__main__': unittest.main()
