"""Multi-cycle scheduler, partial delivery and finite-review regressions."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from studio_runtime.workflow import Workflow
from studio_runtime.cognitive_operations import prepare, packet, validate_response
from studio_runtime.contracts import digest
from studio_runtime.work_history import project, validate_progress
from test_input_lifecycle import empty, output


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parents[1]/'.validation')
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name)/'run'
        self.w = Workflow.create(self.path, 'Synthetic wait lifecycle')
        with self.w._tx() as (con,s):
            s['paused'] = True
            for j in s['jobs'].values(): j['state']='superseded'
            for i in s['inquiries'].values(): i['state']='closed'
            s['topics']['ROOT']['dirty']=False
            self.w.parliament._put(con,'A','topic',parent='ROOT',actor='A.AUTHOR')
            self.w.parliament._put(con,'A.v1','revision',topic='A',parent='ROOT',questions=['Analyze changes'],
                I=[],candidates=[],previous=None,continuity='fresh',unresolved_previous='',relation='complement')
            con.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
            s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',
                new_returns=[],consumed_returns=[],dirty=False)
            s['user_inputs'].append(dict(id='I-old',kind='coordination',topic_id='A',text='Previously delivered report'))
            s['input_receipts']['I-old']={'state':'processed'}
            s['question_handoffs']=[dict(id='H-old',question_id='Q-old',recipient='A',coordinator='ROOT',
                source_job='old',state='delivered',input_id='I-old'),
                dict(id='H-pending',question_id='Q-pending',recipient='A',coordinator='ROOT',
                source_job='old',state='awaiting_answer')]

    def state(self):
        with self.w._tx() as (_,s): return deepcopy(s)

    def job(self,role='author',topic='A'):
        with self.w._tx() as (c,s):
            jid = self.w._author(c,s,topic) if role=='author' else self.w._parent(c,s,topic)
            return deepcopy(s['jobs'][jid]['spec'])

    def answer(self,spec,status='wait',no_change=False):
        out=empty(spec['schema'])
        out.update(status=status,ans='Current bounded findings remain available',con='Later report still required')
        out['input_uses']=[dict(input_id=i,use='Read actual arrival',effect='Retain the remaining dependency',disposition='processed')
            for i in json.loads(spec['prompt'])['task']['input_obligations']]
        if no_change: out['progress']={'kind':'no_change','reason':'No new conclusion or action'}
        return out

    def apply(self,spec,out):
        r=self.w.apply(spec['job_id'],out,defer_progress=True)
        self.assertEqual(r['status'],'applied',self.state()['jobs'][spec['job_id']].get('error'))
        return r

    def stable(self,ticks=20):
        count=len(self.state()['jobs'])
        self.w.resume()
        for _ in range(ticks):
            self.assertEqual(self.w.advance(),[])
        self.assertEqual(len(self.state()['jobs']),count)
        self.assertEqual(self.state()['state'],'waiting')

    def test_reproduce_delivered_plus_pending_then_eight_arrivals_and_restarts(self):
        spec=self.job();self.apply(spec,self.answer(spec))
        self.stable(30)
        for n in range(8):
            before=len(self.state()['jobs'])
            entry=self.w.input('Independent fixture event '+str(n),topic_id='A',profile_scope='none',submission_id='cycle-000'+str(n))
            pending=self.w.advance()
            self.assertEqual([(j.role,j.metadata['topic_id']) for j in pending],[('author','A')])
            spec=pending[0].to_dict()
            self.assertIn(entry['id'],json.loads(spec['prompt'])['task']['input_obligations'])
            self.apply(spec,self.answer(spec,no_change=True))
            self.assertEqual(self.state()['input_receipts'][entry['id']]['state'],'processed')
            self.w=Workflow(self.path)
            self.stable(20)
            self.w.input('Independent fixture event '+str(n),topic_id='A',profile_scope='none',submission_id='cycle-000'+str(n))
            self.stable(3)
            self.assertEqual(len(self.state()['jobs']),before+1)
        with self.w._tx() as (c,s):
            full=self.w._work_history(c,s,'A','author',archived=True)
            visible=self.w._work_history(c,s,'A','author')
        self.assertEqual(len(full),9)
        self.assertEqual(len(visible),1)
        self.assertEqual(visible[0]['following_receipts']['count'],8)
        self.assertFalse([d for d in self.state()['diagnostics'] if d['kind']=='wait_dependency_resolved_before_apply'])

    def test_resolution_during_call_wakes_once_and_never_from_old_resolution(self):
        spec=self.job()
        with self.w._tx() as (_,s):
            s['question_handoffs'][1].update(state='delivered',input_id='I-new')
            s['user_inputs'].append(dict(id='I-new',topic_id='A',kind='coordination',text='Actual arrived report'))
            s['question_handoffs'].append(dict(id='H-next',question_id='Q-next',recipient='A',coordinator='ROOT',
                source_job='next',state='awaiting_answer'))
        self.apply(spec,self.answer(spec))
        self.w.resume();pending=self.w.advance()
        self.assertEqual(len(pending),1)
        after=pending[0].to_dict();self.apply(after,self.answer(after))
        self.stable(40)
        ds=[d for d in self.state()['diagnostics'] if d['kind']=='wait_dependency_resolved_before_apply']
        self.assertEqual(len(ds),1)
        self.assertEqual([r['id'] for r in ds[0]['resolved_dependencies']],['H-pending'])

    def test_fabricated_delivered_handoff_is_not_a_wakeup(self):
        spec=self.job()
        with self.w._tx() as (_,s): s['question_handoffs'][1].update(state='delivered')
        r=self.w.apply(spec['job_id'],self.answer(spec),defer_progress=True)
        self.assertEqual(r['status'],'rejected')
        self.assertFalse(self.state()['topics']['A'].get('author_refresh_required'))

    def test_generated_report_retrieval_returns_exact_body_and_preserves_scope(self):
        spec=self.job();out=self.answer(spec)
        out['files']=[dict(path='review.txt',content='Actual finite review source: alpha=1')]
        out['tool_requests']=[dict(kind='context.retrieve',purpose='Read own newly generated report',
            arguments=json.dumps(dict(path='@self/review.txt',max_chars=4000)))]
        self.apply(spec,out)
        req=self.state()['tool_requests'][-1]
        result=self.w.context_result(req)
        self.assertEqual(result['outcome'],'succeeded')
        self.assertTrue(result['payload']['complete'])
        self.assertEqual(result['payload']['value']['content'],out['files'][0]['content'])
        self.assertEqual(result['receipt']['generated_path'],'artifacts/'+spec['job_id']+'/review.txt')
        # A later call cannot use the own-output path shortcut to read another
        # call's artifact, even in the same topic; it must use authorized inputs.
        other=self.job();out2=self.answer(other)
        out2['tool_requests']=[dict(kind='context.retrieve',purpose='Invalid path shortcut',
            arguments=json.dumps(dict(path=result['receipt']['generated_path'])))]
        self.apply(other,out2)
        with self.assertRaisesRegex(ValueError,"producing call's scope"):
            self.w.context_result(self.state()['tool_requests'][-1])
        forged=deepcopy(req);forged['arguments']['path']='artifacts/ROOT/secret.txt'
        with self.assertRaisesRegex(ValueError,'persisted original'):
            self.w.context_result(forged)

    def test_checkpoint_returns_and_is_consumed_while_dependency_remains(self):
        from studio_runtime.feedback import open_wave
        with self.w._tx() as (_,s):open_wave(s,'ROOT','wave',['A'])
        spec=self.job();out=self.answer(spec,'checkpoint')
        out['contributions']=[dict(text='Bounded conclusion; not completion of pending handoff',candidate_ids=[])]
        out['files']=[dict(path='phase.txt',content='Available partial result; next report is unknown')]
        self.apply(spec,out)
        self.w.resume()
        for j in self.w.advance():
            self.assertEqual(j.role,'assessment')
            a=empty(j.schema);a.update(judgment='sufficient',reason='Bounded phase only',abstract='Conditional',
                scenario='Synthetic',conditions='Pending input remains',promote=False)
            self.apply(j.to_dict(),a)
        # Memory maintenance is orthogonal; process its actual finite packet.
        pending=self.w.advance()
        for j in pending:
            if j.role=='memory_review':
                m=empty(j.schema)
                m['reviewed_material_ids']=j.metadata['memory_context']['new_material_ids']
                self.apply(j.to_dict(),m)
        parent=next(j for j in self.w.advance() if j.role=='parent')
        task=json.loads(parent.prompt)['task']
        self.assertEqual(task['new_returns'][0]['delivery_kind'],'checkpoint')
        p=output(parent.to_dict(),'wait')
        p['contribution_decisions']=[dict(return_id=r['id'],decision='Use partial result',reason='Await real dependency') for r in task['new_returns']]
        self.apply(parent.to_dict(),p)
        self.assertEqual(self.state()['topics']['A']['state'],'waiting')
        self.stable(30)
        self.assertEqual(self.state()['topics']['ROOT']['feedback_waves'][0]['state'],'open')

    def test_complete_cannot_hide_handoff_but_partial_root_delivery_can_wait(self):
        spec=self.job();r=self.w.apply(spec['job_id'],self.answer(spec,'complete'),defer_progress=True)
        self.assertEqual(r['status'],'rejected')
        with self.w._tx() as (_,s):s['jobs'][spec['job_id']]['state']='superseded'
        p=self.job('parent','ROOT');out=output(p,'checkpoint')
        out['files']=[dict(path='phase.md',content='Partial result. H-pending still needs a real answer.')]
        self.apply(p,out)
        self.assertEqual(self.state()['deliveries'][-1]['state'],'partial')
        self.assertTrue(self.state()['deliveries'][-1]['remaining_dependencies'])
        self.stable()

    def test_history_read_preserves_original_and_refuses_other_role(self):
        spec=self.job();self.apply(spec,self.answer(spec))
        s2=self.job();self.apply(s2,self.answer(s2,no_change=True))
        s3=self.job();out=self.answer(s3)
        out['tool_requests']=[dict(kind='history.read',arguments=json.dumps({'job_id':s2['job_id']}),purpose='Inspect exact old output')]
        self.apply(s3,out)
        req=next(r for r in self.state()['tool_requests'] if r['source_job']==s3['job_id'])
        result=self.w.history_result(req)
        self.assertIn('no_change',result['payload']['content'])
        p=self.job('parent','ROOT');out=output(p,'wait')
        out['tool_requests']=[dict(kind='history.read',arguments=json.dumps({'job_id':s2['job_id']}),purpose='Invalid cross-role request')]
        self.apply(p,out)
        req=next(r for r in self.state()['tool_requests'] if r['source_job']==p['job_id'])
        with self.assertRaisesRegex(ValueError,'outside'):self.w.history_result(req)

    def test_bounded_review_request_result_and_author_uptake(self):
        spec=self.job('parent','ROOT');out=output(spec,'wait')
        out['files']=[dict(path='source.txt',content='alpha=1; beta=2')]
        self.apply(spec,out)
        spec=self.job('parent','ROOT');task=json.loads(spec['prompt'])['task']
        # Use the original snapshot pointer, never a projected array index.
        sha=spec['metadata']['context_projection']['source_sha256']
        original=self.state()['context_snapshots'][sha]
        index=next(n for n,a in enumerate(original['artifacts']) if a['path'].endswith('/source.txt'))
        path=original['artifacts'][index]['path']
        args=dict(operation='bounded_review',brief=dict(situation='Verify two finite entries',observations='',interpretation='',global_question=''),
            sources=[dict(id=path,pointer='/artifacts/'+str(index),window='B')],constraints=[],withheld_sources=[],
            checklist=[dict(id=k,question=k+' equals '+str(n),source_ids=[path]) for n,k in [(1,'alpha'),(2,'beta')]])
        out=output(spec,'wait');out['tool_requests']=[dict(kind='cognition.process',arguments=json.dumps(args),purpose='Explicit finite check')]
        self.apply(spec,out)
        request=next(r for r in self.state()['tool_requests'] if r['source_job']==spec['job_id'])
        self.assertIsNone(self.w.cognitive_result(request))
        s=self.state();processing=s['jobs'][s['cognitive_operations'][request['id']]['active_job']]['spec']
        self.assertIn('alpha=1; beta=2',processing['prompt'])
        answer=empty(processing['schema']);answer.update(processing='Compared the two exact values',
            source_uses=[dict(source_id=path,use='Read both values',effect='Both match')],
            items=[dict(id=k,status='no_change',source_ids=[path],finding=k+' matches',remaining='') for k in ['alpha','beta']])
        self.apply(processing,answer)
        returned=self.w.cognitive_result(request)
        self.assertEqual(returned['payload']['review_status'],'reviewed')
        entry=self.w.tool_result(request['id'],returned,source='fixture',defer_progress=True)
        spec=self.job('parent','ROOT');out=output(spec,'wait')
        self.assertIn(entry['id'],json.loads(spec['prompt'])['task']['input_obligations'])
        self.apply(spec,out)
        self.assertEqual(self.state()['cognitive_operations'][request['id']]['state'],'consumed')
        self.assertEqual(self.state()['input_receipts'][entry['id']]['state'],'processed')
        self.stable()

    def test_cancelled_handoff_does_not_reopen_on_answer(self):
        with self.w._tx() as (_,s):
            s['question_handoffs'][1]['state']='cancelled'
            self.w._answer_dependencies(s,{'id':'Q-pending'}, {'id':'I-unrelated'})
            self.assertEqual(s['question_handoffs'][1]['state'],'cancelled')

    def test_repeated_unknown_is_one_obligation_with_original_output_preserved(self):
        for _ in range(3):
            spec=self.job();out=self.answer(spec)
            out['unresolved']=[dict(text='Future comparison',condition='On new version',next_action='Compare it')]
            self.apply(spec,out)
        obligations=[o for o in self.state()['obligations'] if o.get('text')=='Future comparison']
        self.assertEqual(len(obligations),1)
        self.assertEqual(obligations[0]['reaffirmations'],2)

    def test_no_change_does_not_trigger_new_user_learning_for_old_use(self):
        from studio_runtime import user_collaboration as user
        with self.w._tx() as (_,s):
            user.record_event(s,'OLD.USER','observation',source='fixture',topic_id='A',content='Preference',allowed_uses=['profile','collaboration'],audience=['*'])
            s['user_inputs'][0]['user_event_id']='OLD.USER'
            s['input_receipts']['I-old']['history']=[]
        spec=self.job();out=self.answer(spec,no_change=True)
        out['input_uses']=[dict(input_id='I-old',use='Same known use',effect='No changed decision',disposition='processed')]
        self.apply(spec,out)
        self.assertFalse([e for e in self.state()['user_collaboration']['events'] if e['kind']=='downstream_use'])

    def test_empty_council_result_requires_explicit_parent_uptake(self):
        with self.w._tx() as (c,s):self.w._inquiry(c,s,'ROOT','Consider alternatives without asserting a completed audit')
        specs=[j['spec'] for j in self.state()['jobs'].values() if j['state']=='pending']
        for spec in specs:
            self.apply(spec,dict(candidates=[],selection_requests=[],outcome='no_new_direction',reason='No distinct candidate in supplied scope'))
        self.w.resume();spec=next(j.to_dict() for j in self.w.advance() if j.role=='parent')
        out=output(spec,'wait');self.assertTrue(out['inquiry_uses'])
        task=json.loads(spec['prompt'])['task'];self.assertEqual(len(task['inquiry_results'][0]['outcomes']),2)
        self.apply(spec,out)
        req=self.state()['inquiries'][task['inquiry_results'][0]['id']]
        self.assertEqual(req['state'],'closed');self.assertEqual(len(req['uptake']),1)
        self.stable()

    def test_child_finishing_into_another_wait_does_not_force_parent_polling(self):
        author=self.job();parent=self.job('parent','ROOT')
        self.assertTrue(json.loads(parent['prompt'])['task']['active_jobs'])
        self.apply(author,self.answer(author))
        self.apply(parent,output(parent,'wait'))
        self.stable(30)
        ds=[d for d in self.state()['diagnostics'] if d['kind']=='wait_dependency_resolved_before_apply']
        self.assertEqual(len(ds),1)
        self.assertEqual(ds[0]['resolved_dependencies'][0]['id'],author['job_id'])
        self.assertFalse(ds[0]['wakeup_required'])

    def test_parent_sealed_wait_receives_final_child_result_after_race(self):
        with self.w._tx() as (_,s):s['question_handoffs'][1]['state']='cancelled'
        author=self.job();parent=self.job('parent','ROOT')
        self.apply(author,self.answer(author,'complete'))
        self.apply(parent,output(parent,'wait'))
        self.w.resume();jobs=self.w.advance()
        self.assertEqual([j.role for j in jobs],['parent'])
        task=json.loads(jobs[0].prompt)['task']
        self.assertEqual(len(task['new_returns']),1)
        self.assertEqual(task['new_returns'][0]['delivery_kind'],'complete')

    def test_maintenance_cannot_erase_delivery_or_force_a_business_call(self):
        with self.w._tx() as (c,s):
            s['question_handoffs'][1]['state']='cancelled'
            s['topics']['A']['state']='returned'
            context=dict(new_material_ids=[],materials=[],available_source_ids=[])
            prompt,schema,packet=self.w._memory_packet(context)
            jid=self.w._job(s,'memory_review','ROOT',prompt,
                dict(member_id=s['members'][0]['id'],memory_context=context,input_packet=packet),schema=schema)
            maintenance=deepcopy(s['jobs'][jid]['spec'])
        parent=self.job('parent','ROOT')
        self.assertFalse(json.loads(parent['prompt'])['task']['active_jobs'])
        self.apply(parent,output(parent,'quiesce'))
        self.w.resume();self.w.advance()
        self.assertEqual(self.state()['state'],'active')
        self.apply(maintenance,empty(maintenance['schema']))
        count=len(self.state()['jobs'])
        for _ in range(15):self.assertEqual(self.w.advance(),[])
        self.assertEqual(self.state()['state'],'quiescent')
        self.assertEqual(len(self.state()['jobs']),count)

    def test_parent_waits_for_real_handoff_refresh_before_child_job_exists(self):
        with self.w._tx() as (_,s):
            s['question_handoffs'][1].update(state='delivered',input_id='I-new')
            self.w._interaction_message(s,'A','Actual new report pending dispatch','fixture')
        p=self.job('parent','ROOT');self.apply(p,output(p,'wait'))
        self.assertIn('author_refresh',[d['kind'] for d in self.state()['topics']['ROOT']['wait_dependencies']])
        self.w.resume();pending=self.w.advance()
        self.assertEqual([j.role for j in pending],['author'])

    def test_parent_cannot_deliver_before_registered_child_refresh_and_input(self):
        with self.w._tx() as (_,s):
            s['question_handoffs'][1]['state']='cancelled'
            self.w._interaction_message(s,'A','Actual scoped arrival','fixture')
        p=self.job('parent','ROOT');out=output(p,'deliver')
        out['files']=[dict(path='premature.md',content='Must not become final')]
        r=self.w.apply(p['job_id'],out,defer_progress=True)
        self.assertEqual(r['status'],'rejected')
        self.assertIn('awaiting dispatch',self.state()['jobs'][p['job_id']]['error'])


class ReviewTests(unittest.TestCase):
    def plan(self):
        original={'artifacts':[{'path':'artifacts/fixture/check.txt','content':'1 alpha exists; 2 beta is absent.'}]}
        args={'operation':'bounded_review','brief':dict(situation='Check two obligations',observations='',interpretation='',global_question=''),
            'sources':[dict(id='artifacts/fixture/check.txt',pointer='/artifacts/0',window='B')],
            'constraints':[],'withheld_sources':[],
            'checklist':[dict(id=str(n),question=q,source_ids=['artifacts/fixture/check.txt']) for n,q in [(1,'alpha exists?'),(2,'beta exists?')]]}
        return prepare(dict(id='T',topic_id='A',arguments=args),original,source_job='J',source_sha=digest(original))

    def test_required_materials_and_each_result_are_in_actual_packet(self):
        plan=self.plan();prompt,schema,_=packet(plan);v=json.loads(prompt)
        self.assertEqual(v['B'][0]['content']['content'],'1 alpha exists; 2 beta is absent.')
        out=empty(schema);out.update(processing='Checked two items',source_uses=[dict(source_id=plan['sources'][0]['id'],use='Read',effect='Check')])
        out['items']=[dict(id=str(n),status='no_change',source_ids=[plan['sources'][0]['id']],finding='Compared actual text',remaining='') for n in [1,2]]
        validate_response(out,{'prompt':prompt})
        bad=deepcopy(out);bad['items']=[]
        with self.assertRaisesRegex(ValueError,'every checklist'):validate_response(bad,{'prompt':prompt})
        bad=deepcopy(out);bad['items'][0]['source_ids']=[]
        with self.assertRaisesRegex(ValueError,'every required source'):validate_response(bad,{'prompt':prompt})
        bad=deepcopy(out);bad['items'][0].update(status='insufficient_material',remaining='Need later version')
        validate_response(bad,{'prompt':prompt})

    def test_new_actions_cannot_be_hidden_as_no_change(self):
        with self.assertRaisesRegex(ValueError,'new work'):
            validate_progress(dict(progress={'kind':'no_change','reason':'Same'},status='wait',files=[{'content':'New'}]))

    def test_unique_advances_are_not_semantically_deduplicated(self):
        records=[dict(job_id='J'+str(i),revision='R',output={'ans':'Version '+str(i)}) for i in range(50)]
        self.assertEqual(len(project(records)),50)


if __name__=='__main__':unittest.main()
