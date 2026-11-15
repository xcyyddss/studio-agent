"""Offline failure/recovery regressions; native acceptance is a separate mandatory suite."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from studio_runtime.contracts import atomic_json
from studio_runtime.sandbox_runtime import diagnosis
from studio_runtime.tool_adapters import LocalToolAdapter, ToolError
from studio_runtime.tool_recovery import test_status
from studio_runtime.workflow import Workflow
from studio_runtime.service import StudioService
from test_input_lifecycle import output


class SandboxRecoveryTests(unittest.TestCase):
    def setUp(self):
        scratch=Path(__file__).resolve().parents[1]/'.validation';scratch.mkdir(exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(self.tmp.cleanup)
        self.service=StudioService(self.tmp.name);self.addCleanup(self.service.close)
        self.service.create('Synthetic test recovery',run_id='fixture',sandbox_tests=True)
        self.path=Path(self.tmp.name)/'fixture';self.w=Workflow(self.path)
        with self.w._tx() as (_,s):
            s['paused']=True
            for j in s['jobs'].values():j['state']='superseded'
            for r in s['inquiries'].values():r['state']='closed'
        (self.path/'artifacts').mkdir(exist_ok=True)
        (self.path/'artifacts/test_example.py').write_text('import unittest\nclass T(unittest.TestCase):\n def test_x(self): self.assertEqual(1,1)\n',encoding='utf-8')
        with self.w._tx() as (_,s):
            self.request={'id':self.w._id(s,'TOOL'),'kind':'sandbox.python_tests','state':'requested','topic_id':'ROOT','source_job':'FIXTURE',
                'purpose':'Synthetic failure recovery','arguments':{'files':[{'path':'artifacts/test_example.py','destination':'test_example.py'}]}}
            s['tool_requests'].append(deepcopy(self.request))
        self.adapter=LocalToolAdapter(self.path,enable_sandbox_tests=True)

    def state(self):
        with self.w._tx() as (_,s):return deepcopy(s)

    def failed(self):
        failed={'available':False,'reason':'Synthetic missing MXC', 'diagnostic':diagnosis('mxc_unavailable','Synthetic prerequisite failure')}
        with patch('studio_runtime.sandbox_runtime.probe',return_value=failed):r=self.adapter.execute(self.request)
        self.w.tool_result(self.request['id'],r,defer_progress=True)
        return r

    def test_preflight_failure_is_not_test_failure_and_is_replayable_without_launch(self):
        r=self.failed()
        self.assertEqual(r['outcome'],'failed');self.assertFalse(r['diagnostic']['test_code_started'])
        folder=self.path/r['receipt']['receipt_path'];folder=folder.parent
        self.assertFalse((folder/'sandbox-invocation.json').exists())
        with patch('studio_runtime.sandbox_runtime.probe',side_effect=AssertionError('no reexecution')):
            self.assertEqual(self.adapter.execute(self.request),r)
        self.assertEqual(test_status(self.state())[0]['diagnostic']['code'],'mxc_unavailable')

    def test_retry_preserves_old_bytes_coalesces_clicks_and_requires_result_uptake(self):
        r=self.failed();old_files={str(f):f.read_bytes() for f in (self.path/r['receipt']['receipt_path']).parent.rglob('*') if f.is_file()}
        with self.w._tx() as (c,s):old_job=self.w._parent(c,s,'ROOT')
        new=self.service.retry_tool('fixture',self.request['id'],reason='Environment repaired')
        self.assertEqual(self.state()['jobs'][old_job]['state'],'superseded')
        again=self.service.retry_tool('fixture',self.request['id'],reason='Repeated HTTP click')
        self.assertEqual(new['new_request_id'],again['new_request_id'])
        req=next(r for r in self.state()['tool_requests'] if r['id']==new['new_request_id'])
        self.assertEqual(req['retry_of'],self.request['id']);self.assertTrue(req['expected_inputs'])
        # A deterministic returned test fixture exercises consumption. Native
        # runner and real model uptake are validated independently end-to-end.
        returned={'outcome':'succeeded','payload':{'reported_test_count':1},'receipt':{'evidence_kind':'fixture'}}
        entry=self.w.tool_result(req['id'],returned,source='fixture',defer_progress=True)
        with self.w._tx() as (c,s):jid=self.w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
        task=json.loads(spec['prompt'])['task'];self.assertIn(entry['id'],task['input_obligations'])
        out=output(spec);self.assertEqual(self.w.apply(jid,out,defer_progress=True)['status'],'applied')
        self.assertEqual(test_status(self.state())[-1]['uptake']['state'],'processed')
        for f,raw in old_files.items():self.assertEqual(Path(f).read_bytes(),raw)

    def test_retries_pin_original_bytes_at_planning_and_at_execution(self):
        self.failed();p=self.path/'artifacts/test_example.py';original=p.read_bytes();p.write_text('changed',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'changed'):self.w.retry_tool(self.request['id'],reason='retry')
        p.write_bytes(original);new=self.w.retry_tool(self.request['id'],reason='retry')
        p.write_text('changed again',encoding='utf-8')
        req=next(r for r in self.state()['tool_requests'] if r['id']==new['new_request_id'])
        with patch('studio_runtime.sandbox_runtime.probe',side_effect=AssertionError('must fail before probe')):r=self.adapter.execute(req)
        self.assertEqual(r['outcome'],'failed');self.assertIn('bytes changed',r['error'])

    def test_legacy_failure_recovery_keeps_original_result(self):
        # Represents the historical prelaunch failure format exactly; no new diagnostic invented.
        with patch('studio_runtime.sandbox_runtime.probe',side_effect=ToolError('junctions and reparse points are not permitted')):
            result=self.adapter.execute(self.request)
        folder=self.path/result['receipt']['receipt_path'];folder=folder.parent
        from studio_runtime.contracts import digest
        old=json.loads((folder/'result.json').read_text(encoding='utf-8'));old.pop('diagnostic',None)
        atomic_json(folder/'result.json',old)
        receipt=json.loads((folder/'receipt.json').read_text(encoding='utf-8'));receipt['result_sha256']=digest(old);atomic_json(folder/'receipt.json',receipt)
        self.w.tool_result(self.request['id'],dict(old,receipt=receipt),defer_progress=True)
        raw=(folder/'result.json').read_bytes();new=self.w.retry_tool(self.request['id'],reason='Version upgrade repairs installer aliases')
        self.assertTrue(new['new_request_id']);self.assertEqual(raw,(folder/'result.json').read_bytes())

    def test_uncertain_requires_explicit_decision_and_tamper_is_rejected(self):
        r=self.failed();folder=(self.path/r['receipt']['receipt_path']).parent
        from studio_runtime.contracts import digest
        saved=json.loads((folder/'result.json').read_text(encoding='utf-8'));saved['outcome']='uncertain';atomic_json(folder/'result.json',saved)
        receipt=json.loads((folder/'receipt.json').read_text(encoding='utf-8'));receipt.update(outcome='uncertain',result_sha256=digest(saved));atomic_json(folder/'receipt.json',receipt)
        with self.w._tx() as (_,s):next(x for x in s['user_inputs'] if x['request_id']==self.request['id'])['value']=dict(saved,receipt=receipt)
        with self.assertRaisesRegex(ValueError,'allow_uncertain'):self.w.retry_tool(self.request['id'],reason='retry')
        self.assertTrue(self.w.retry_tool(self.request['id'],reason='explicit decision',allow_uncertain=True)['new_request_id'])

    def test_readonly_checkpoint_and_disabled_run_refuse_retry(self):
        self.failed();app=json.loads((self.path/'app.json').read_text(encoding='utf-8'));app['checkpoint_read_only']=True;atomic_json(self.path/'app.json',app)
        with self.assertRaises(ValueError):self.service.retry_tool('fixture',self.request['id'],reason='retry')
        app['checkpoint_read_only']=False;app['sandbox_tests']=False;atomic_json(self.path/'app.json',app)
        with self.assertRaisesRegex(ValueError,'not enabled'):self.service.retry_tool('fixture',self.request['id'],reason='retry')

    def test_execution_recovery_is_scoped_durable_wait_not_false_completion(self):
        self.failed();self.w=Workflow(self.path)
        with self.w._tx() as (c,s):
            jid=self.w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
            self.assertEqual(len(self.w._topic_task(c,s,'ROOT')['tool_recoveries']),1)
        self.assertEqual(len(json.loads(spec['prompt'])['task']['tool_recoveries']),1)
        out=output(spec,action='wait')
        self.assertEqual(self.w.apply(jid,out,defer_progress=True)['status'],'applied')
        self.assertEqual(self.state()['topics']['ROOT']['state'],'waiting')
        with self.w._tx() as (c,s):jid=self.w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
        invalid=self.w.apply(jid,output(spec,action='quiesce'),defer_progress=True)
        self.assertEqual(invalid['status'],'rejected')
        self.assertIn('execution recovery',str(self.state()['jobs'][jid]['error']))
        record=self.service.dismiss_tool_recovery('fixture',self.request['id'],reason='Test no longer needed; cannot claim success')
        self.assertEqual(self.state()['tool_recoveries'][0]['state'],'dismissed')
        self.assertIn(record['id'],[i['id'] for i in self.state()['user_inputs']])
        with self.w._tx() as (c,s):
            jid=self.w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
        self.assertIn(record['id'],json.loads(spec['prompt'])['task']['input_obligations'])

    def test_plain_assertion_failure_does_not_create_operator_recovery(self):
        self.w.tool_result(self.request['id'],{'outcome':'failed','diagnostic':diagnosis(
            'test_failure','AssertionError',phase='execution',executed=True)},defer_progress=True)
        self.assertFalse(self.state().get('tool_recoveries'))

    def test_ledger_tampering_blocks_explicit_retry(self):
        r=self.failed();folder=(self.path/r['receipt']['receipt_path']).parent
        saved=json.loads((folder/'result.json').read_text(encoding='utf-8'));saved['error']='altered'
        atomic_json(folder/'result.json',saved)
        with self.assertRaisesRegex(ValueError,'integrity mismatch'):
            self.w.retry_tool(self.request['id'],reason='retry')

    def test_crash_before_request_write_can_only_be_explicitly_retried(self):
        directory=self.adapter._ledger(self.request['id']);directory.mkdir()
        result=self.adapter.execute(self.request)
        self.assertEqual(result['outcome'],'uncertain')
        self.w.tool_result(self.request['id'],result,defer_progress=True)
        with self.assertRaisesRegex(ValueError,'allow_uncertain'):
            self.w.retry_tool(self.request['id'],reason='retry')
        self.assertTrue(self.w.retry_tool(self.request['id'],reason='Explicitly restart incomplete intent',allow_uncertain=True))

    def test_retry_closes_original_duty_and_new_setup_failure_remains_visible(self):
        self.failed();new=self.w.retry_tool(self.request['id'],reason='retry')
        r={'outcome':'failed','diagnostic':diagnosis('mxc_unavailable','Still unavailable')}
        self.w.tool_result(new['new_request_id'],r,defer_progress=True)
        duties=self.state()['tool_recoveries']
        self.assertEqual([d['state'] for d in duties],['resolved','awaiting_action'])
        self.assertEqual(duties[-1]['id'],new['new_request_id'])

    def test_retry_replaces_failed_old_control_without_losing_failure_evidence(self):
        self.failed()
        with self.w._tx() as (c,s):
            jid=self.w._parent(c,s,'ROOT')
            s['jobs'][jid].update(state='failed',error={'message':'Preserved earlier wait import failure'})
            original=deepcopy(s['jobs'][jid]['spec'])
        self.w.retry_tool(self.request['id'],reason='Repair execution and rebuild current owner input')
        job=self.state()['jobs'][jid]
        self.assertEqual(job['state'],'superseded')
        self.assertEqual(job['spec'],original)
        self.assertEqual(job['error']['message'],'Preserved earlier wait import failure')

    def test_recovery_visibility_and_sealed_wait_resolution_race(self):
        self.failed()
        with self.w._tx() as (c,s):
            for name in ('A','B'):
                self.w.parliament._put(c,name,'topic',parent='ROOT',actor=name+'.AUTHOR')
                self.w.parliament._put(c,name+'.v1','revision',topic=name,parent='ROOT',questions=['Local synthetic work'],
                    I=[],candidates=[],previous=None,continuity='fresh',unresolved_previous='',relation='complement')
                c.execute('INSERT INTO heads VALUES(?,?)',(name,name+'.v1'))
                s['topics'][name]=dict(id=name,parent='ROOT',revision=name+'.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False)
            # Only administrative recovery crosses up to the authorized parent.
            s['tool_recoveries'][0]['topic_id']='A'
            self.assertEqual(self.w._topic_task(c,s,'B')['tool_recoveries'],[])
            jid=self.w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
            self.assertEqual(json.loads(spec['prompt'])['task']['tool_recoveries'][0]['topic_id'],'A')
            # The request was sealed while waiting, but resolution arrives
            # before importing the role output. Honor its valid old wait and
            # schedule fresh input instead of rejecting or declaring completion.
            s['tool_recoveries'][0]['state']='resolved'
        imported=self.w.apply(jid,output(spec,action='wait'),defer_progress=True)
        self.assertEqual(imported['status'],'applied')
        self.assertTrue(self.state()['topics']['ROOT']['dirty'])

    def test_status_does_not_run_probe(self):
        self.failed()
        with patch('studio_runtime.sandbox_runtime.probe',side_effect=AssertionError('polling cannot run probes')):
            status=self.service.status('fixture')
        self.assertEqual(len(status['python_tests']),1)

    def test_health_probe_persists_without_starting_models(self):
        with patch('studio_runtime.sandbox_runtime.probe',return_value={'available':False,'reason':'Synthetic failure'}):
            result=self.service.check_sandbox('fixture')
        self.assertFalse(result['available']);self.assertTrue((self.path/result['record_path']).is_file())
        self.assertFalse(self.service.threads)
