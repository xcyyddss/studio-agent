"""Real fresh calls across three handoff rounds, with idle/restart assertions.

Only synthetic task content is used. Setup records are labelled fixtures;
all runtime author/parent/assessment/processing/maintenance outputs are real.
"""
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid

PROJECT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(PROJECT),str(PROJECT/'tests')]
from studio_runtime.contracts import JobSpec,atomic_json
from studio_runtime.provider import CodexProvider
from studio_runtime.service import StudioService
from studio_runtime.workflow import Workflow
from test_input_lifecycle import output

resume=len(sys.argv)>1
base=Path(sys.argv[1]).resolve() if resume else PROJECT/'.validation'/('real-waits-'+uuid.uuid4().hex[:8])
assert base.resolve().is_relative_to((PROJECT/'.validation').resolve())
base.mkdir(parents=True,exist_ok=resume)
goal=('合成协议验收任务：形成一份三项配置的审阅报告。已设独立作者A与两次后续用户输入，不增设议题。'
 'ROOT只负责把自己收到的三项配置通过question_handoffs报告给A、承接A回传并最终交付。'
 'A按已给配置工作：初始已知alpha=1；用户将分两轮提供beta和gamma。在任一后续输入尚未到达时，'
 '用checkpoint交付这一阶段实际可用的简短结论，同时等待已有handoff；不要重复提问、查询状态或自行调查。'
 '后续资料全部到达后，A必须用cognition.process的bounded_review，以实际报告正文为sources并列出三项检查：'
 'alpha=1、beta=2、gamma=3，收到逐项结果后明确承接，写入最终文件并complete。'
 'ROOT只在三项均收到且A最终回传已经处理后deliver。这里不运行代码、不联网、不请求其他开放加工或用户画像咨询。'
 '等待是正常执行状态；不要为保持运行重复创建工具或重复阶段成果。')
service=StudioService(base)
if not resume:service.create(goal,run_id='fixture',web_search='disabled')
w=Workflow(base/'fixture');provider=CodexProvider({'web_search':'disabled'})
calls=json.loads((base/'calls.json').read_text(encoding='utf-8')) if resume else []
idle=json.loads((base/'idle.json').read_text(encoding='utf-8')) if resume else []
def state():
    with w._tx() as (_,s):return deepcopy(s)
def fixture_parent(out=None):
    with w._tx() as (c,s):jid=w._parent(c,s,'ROOT');spec=deepcopy(s['jobs'][jid]['spec'])
    value=output(spec,'wait')
    if out:value.update(out)
    assert w.apply(jid,value,defer_progress=True)['status']=='applied'
    return spec
if not resume:
    with w._tx() as (c,s):
        s['paused']=True
        for j in s['jobs'].values():j['state']='superseded'
        for i in s['inquiries'].values():i['state']='closed'
        w.parliament._put(c,'A','topic',parent='ROOT',actor='A.AUTHOR')
        w.parliament._put(c,'A.v1','revision',topic='A',parent='ROOT',questions=[goal],I=[],candidates=[],previous=None,
            continuity='fresh',unresolved_previous='',relation='complement')
        c.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
        s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False)
    first=fixture_parent({'questions':[dict(text='Provide '+key,why='Synthetic known missing configuration',blocking=True,guidance_ids=[],attachments=[])
        for key in ('alpha','beta','gamma')]})
    questions=sorted((q for q in state()['questions'].values() if q['source_job']==first['job_id']),key=lambda q:int(q['id'].split('.')[-1]))
    w.input('alpha=1',question_id=questions[0]['id'],source='fixture',profile_scope='none')
    fixture_parent({'question_handoffs':[dict(question_id=q['id'],recipient='A',reason='Single source coordination',
        summary='Actual synthetic answer states alpha=1' if n==0 else '',attachments=[]) for n,q in enumerate(questions)]})
else:
    questions=sorted(state()['questions'].values(),key=lambda q:int(q['id'].split('.')[-1]))[:3]
    recoveries=json.loads((base/'resume-repairs.json').read_text(encoding='utf-8')) if (base/'resume-repairs.json').exists() else []
    for jid,j in state()['jobs'].items():
        if j['state']=='failed':
            recoveries.append({'original':jid,'replacement':w.retry(jid),'error':j.get('error')})
    atomic_json(base/'resume-repairs.json',recoveries)
w.resume()

def drain():
    # Assertion guard against an unbounded test run, never a production stop rule.
    for _ in range(60):
        service._execute_tools(base/'fixture',w)
        jobs=w.advance()
        if not jobs:
            assert not [r for r in state()['tool_requests'] if r['state']=='requested'],state()['tool_requests']
            return
        job=sorted(jobs,key=lambda j:int(j.job_id.split('.')[-1]))[0]
        aid='real-'+uuid.uuid4().hex
        print('CALL',job.job_id,job.role,flush=True)
        result=provider.execute(job,aid,base/'fixture'/'attempts'/aid);result['result_id']=aid
        imported=w.apply(result,defer_progress=True)
        calls.append({'job':job.job_id,'role':job.role,'outcome':result['outcome'],'import':imported,
            'action':result.get('payload',{}).get('status',result.get('payload',{}).get('action')),
            'model':result.get('receipt',{}).get('model'),'effort':result.get('receipt',{}).get('reasoning_effort')})
        atomic_json(base/'calls.json',calls);print('RESULT',json.dumps(calls[-1]),flush=True)
        assert imported['status']=='applied',state()['jobs'][job.job_id].get('error',result)
    raise AssertionError('Acceptance did not settle; investigate extra jobs')

def stable(label,terminal=False):
    global w
    count=len(state()['jobs']);w=Workflow(base/'fixture')
    for _ in range(40):
        service._execute_tools(base/'fixture',w)
        assert w.advance()==[], 'idle scheduler generated an unexpected job'
    s=state();assert len(s['jobs'])==count
    assert s['state'] in ({'delivered','quiescent'} if terminal else {'waiting'}),s['state']
    idle.append({'phase':label,'polls_after_restart':40,'jobs_before':count,'jobs_after':len(s['jobs']),'state':s['state']})
    atomic_json(base/'idle.json',idle);print('IDLE',label,s['state'],count,flush=True)

if not idle:
    drain();stable('alpha only')
assert any(k=='checkpoint' for k in state().get('return_kinds',{}).values())
for index,key in [(1,'beta'),(2,'gamma')]:
    if any(x['phase']==key for x in idle):continue
    if state()['questions'][questions[index]['id']]['state']=='open':
        w.input(key+'='+str(index+1),question_id=questions[index]['id'],source='fixture',profile_scope='none')
    drain();stable(key,terminal=index==2)
s=state();reviews=[p for p in s.get('cognitive_operations',{}).values() if p['operation']=='bounded_review']
assert reviews and all(p['state']=='consumed' for p in reviews)
assert all(len(p['phases'][-1]['output']['items'])==3 for p in reviews)
assert all(s['input_receipts'][p['input_id']]['state']=='processed' for p in reviews)
assert not [j for j in s['jobs'].values() if j['state']=='failed']
atomic_json(base/'acceptance.json',{'calls':calls,'idle':idle,'state':s['state'],
    'review_ids':[p['id'] for p in reviews],'return_kinds':s['return_kinds'],'deliveries':s['deliveries'],
    'boundary':'Synthetic setup; actual fresh model processing, handoffs, review results, uptake and idle scheduler/restart checks.'})
print('VERIFIED',base,flush=True);service.close()
