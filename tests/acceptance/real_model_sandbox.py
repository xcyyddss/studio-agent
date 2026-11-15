"""Synthetic setup-failure injection -> explicit retry -> real MXC -> fresh model uptake."""
import json,sys,uuid
from pathlib import Path
from unittest.mock import patch
PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT))
from studio_runtime.service import StudioService
from studio_runtime.workflow import Workflow
from studio_runtime.provider import CodexProvider
from studio_runtime.contracts import JobSpec,atomic_json
from studio_runtime.sandbox_runtime import diagnosis

base=PROJECT/'.validation'/('real-sandbox-'+uuid.uuid4().hex[:8])
base.mkdir(parents=True)
service=StudioService(base)
goal='合成执行器验收。独立作者A编写一个求两整数之和的Python函数和unittest（检查7+11=18与负数），使用sandbox.python_tests实际执行，并明确承接结果。无需搜索、拆分、评审或用户问题。若执行器准备失败，明确记录不可验证，等待验收台修复后重试原工具请求，不新增相同请求；修复不需要你操作。只有真实测试成功返回后才能完成。'
service.create(goal,run_id='fixture',sandbox_tests=True,web_search='disabled')
root=base/'fixture';w=Workflow(root)
def state():
    with w._tx() as (_,s):return json.loads(json.dumps(s))
with w._tx() as (c,s):
    s['paused']=True
    for j in s['jobs'].values():j['state']='superseded'
    for r in s['inquiries'].values():r['state']='closed'
    w.parliament._put(c,'A','topic',parent='ROOT',actor='A.AUTHOR')
    w.parliament._put(c,'A.v1','revision',topic='A',parent='ROOT',questions=[goal],I=[],candidates=[],previous=None,
        continuity='fresh',unresolved_previous='',relation='complement')
    c.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
    s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False)
provider=CodexProvider({'web_search':'disabled'});calls=[]
def call():
    with w._tx() as (c,s):jid=w._author(c,s,'A');job=JobSpec(**s['jobs'][jid]['spec'])
    aid='real-'+uuid.uuid4().hex
    print('CALL',jid,flush=True)
    result=provider.execute(job,aid,root/'attempts'/aid);result['result_id']=aid
    imported=w.apply(result,defer_progress=True)
    calls.append({'job':jid,'outcome':result['outcome'],'import':imported,
        'model':result.get('receipt',{}).get('model'),'effort':result.get('receipt',{}).get('reasoning_effort')})
    print('RESULT',json.dumps(calls[-1]),flush=True)
    atomic_json(base/'calls.json',calls)
    assert imported['status']=='applied',imported
    return result
first=call()
pending=[r for r in state()['tool_requests'] if r['state']=='requested']
assert len(pending)==1 and pending[0]['kind']=='sandbox.python_tests',pending
original=pending[0]
failure={'available':False,'reason':'Synthetic acceptance fixture: unavailable during the first preflight',
    'evidence_kind':'fixture','diagnostic':diagnosis('mxc_unavailable','Injected preflight failure; no test launched')}
with patch('studio_runtime.sandbox_runtime.probe',return_value=failure):service._execute_tools(root,w)
failed_input=next(i for i in state()['user_inputs'] if i.get('request_id')==original['id'])
assert failed_input['value']['outcome']=='failed'
middle=call()
assert middle['payload']['status']!='complete'
assert not [r for r in state()['tool_requests'] if r['state']=='requested']
retry=service.retry_tool('fixture',original['id'],reason='Acceptance host now ready; retry exact original test files')
service._execute_tools(root,w)  # No patch: real native MXC execution.
returned=next(i for i in state()['user_inputs'] if i.get('request_id')==retry['new_request_id'])
assert returned['value']['outcome']=='succeeded',returned
assert returned['value']['payload']['reported_test_count']>=2
last=call()
assert last['payload']['status']=='complete',last['payload']
s=state();uptake=s['input_receipts'][returned['id']]
assert uptake['state']=='processed',uptake
assert failed_input['id'] in s['input_receipts']
report={'path':str(base),'calls':calls,'fixture':'First preflight failure deliberately injected; it is not evidence of a real MXC failure.',
    'retry':retry,'test_result':returned,'uptake':uptake,'summary':s['topics']['A']['summary'],
    'boundary':'Only synthetic task material was sent. Native retry, fresh model work, output application and input receipts are real.'}
atomic_json(base/'acceptance.json',report)
print('VERIFIED',base,flush=True)
service.close()

