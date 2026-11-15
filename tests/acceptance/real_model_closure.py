"""Real Astra/xhigh calls: internal material delivery and successive user gates.

Setup/answers are labelled synthetic. All reasoning, questions, dependencies,
partial/final reports and uptake are actual independent model outputs. There is
no exploration-round cutoff; a stalled/failed state fails acceptance explicitly.
"""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
import json
import hashlib
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from studio_runtime.workflow import Workflow
from studio_runtime.service import StudioService
from studio_runtime.provider import CodexProvider
from studio_runtime.contracts import atomic_json

base=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT/'.validation'/('closure-'+uuid.uuid4().hex[:8])
assert base.is_relative_to((ROOT/'.validation').resolve())
base.mkdir(parents=True,exist_ok=True)
service=StudioService(base)
calls=json.loads((base/'calls.json').read_text(encoding='utf8')) if (base/'calls.json').exists() else []
idle=json.loads((base/'idle.json').read_text(encoding='utf8')) if (base/'idle.json').exists() else []
goal=('合成验收目标：为一个本地资料保存工具形成可交付的范围与验收方案。已有ROOT和独立作者A。'
      'ROOT持有精确policy.txt原件，A尚未获得正文；A负责研究并交付方案，ROOT负责组织、正式递送材料和阶段交付。'
      '阶段一：A取得原件后形成可用的初步报告，并取得用户对部署环境的回答；在回答到达之前，交付已有成果并合法等待。'
      '阶段二：部署环境明确后进一步形成适合环境的方案，再取得用户对导出格式的回答；在第二份回答到达前再次交付已有成果并等待。'
      '阶段三：两份回答已知后进行必要核对和最终交付。未知信息不能自行代答，问题由系统决定怎样表述和协调。'
      '任务只要求这份资料保存方案与验收设计，不要求真的实现软件，不需要联网或执行代码。'
      '可自行提出有价值的新问题和选择认知加工方式；所有动作须服务当前交付，不以探索轮数为停止条件。')
if not (base/'run'/'workflow.db').exists():
    service.create(goal,run_id='run',web_search='disabled')
    w=Workflow(base/'run')
    with w._tx() as (c,s):
        s['paused']=True
        for j in s['jobs'].values():j['state']='superseded'
        for r in s['inquiries'].values():r['state']='closed'
        s['jobs']['FIXTURE.SOURCE']={'state':'applied','created_at':'setup',
            'spec':{'job_id':'FIXTURE.SOURCE','role':'parent','metadata':{'topic_id':'ROOT'}}}
        c.execute('INSERT INTO artifacts VALUES(?,?,?)',('artifacts/FIXTURE.SOURCE/policy.txt',
            'Synthetic policy original.\nMODE=read_only applies to user source files: never modify or delete them.\n'
            'The tool may create its own reports in a user-selected output directory.\n'
            'RETENTION_DAYS=17 applies only to app-generated cached reports, measured from creation in UTC; expired cache may be deleted, never originals.\n'
            'No remote uploads.\nDeployment environment and export format must come from later fixture replies.\n', 'FIXTURE.SOURCE'))
        w.parliament._put(c,'A','topic',parent='ROOT',actor='A.AUTHOR')
        w.parliament._put(c,'A.v1','revision',topic='A',parent='ROOT',questions=[goal],I=[],candidates=[],previous=None,
            continuity='fresh',unresolved_previous='',relation='implementation')
        c.execute('INSERT INTO heads VALUES(?,?)',('A','A.v1'))
        s['topics']['A']=dict(id='A',parent='ROOT',revision='A.v1',state='active',summary='',new_returns=[],consumed_returns=[],dirty=False,author_refresh_required=True)
        s['topics']['ROOT']['dirty']=False
        s['paused']=False
else:
    w=Workflow(base/'run')

def snapshot():
    with w._read_tx() as (_,s):return deepcopy(s)

def drain():
    active={}
    provider=CodexProvider({'web_search':'disabled'})
    with ThreadPoolExecutor(max_workers=6) as pool:
        while True:
            service._execute_tools(base/'run',w)
            w.repair_contract_failures();w.recover_failures()
            jobs=w.advance()
            running={j.job_id for j in active.values()}
            for job in jobs:
                if job.job_id in running or len(active)>=6:continue
                aid='real-'+uuid.uuid4().hex
                future=pool.submit(provider.execute,job,aid,base/'run'/'attempts'/aid)
                active[future]=job
                print('CALL',job.job_id,job.role,len(job.prompt),flush=True)
            if not active:
                s=snapshot()
                failed=[(k,v.get('error')) for k,v in s['jobs'].items() if v['state']=='failed']
                assert not failed,failed
                assert s['state'] in {'waiting','delivered','quiescent'},(s['state'],s['state_reason'])
                return
            done,_=wait(active,timeout=1,return_when=FIRST_COMPLETED)
            for future in done:
                job=active.pop(future);r=future.result()
                r['result_id']=r['attempt_id']
                started=time.monotonic();imported=w.apply(r,defer_progress=True)
                calls.append({'job':job.job_id,'role':job.role,'chars':len(job.prompt),'outcome':r['outcome'],
                    'import':imported,'import_seconds':time.monotonic()-started,'model':r['receipt'].get('model'),
                    'effort':r['receipt'].get('reasoning_effort'), 'action':r.get('payload',{}).get('status',r.get('payload',{}).get('action'))})
                atomic_json(base/'calls.json',calls)
                print('RESULT',json.dumps(calls[-1],ensure_ascii=False),flush=True)

def stable(label):
    global w
    w=Workflow(base/'run')
    count=len(snapshot()['jobs'])
    for _ in range(30):
        service._execute_tools(base/'run',w)
        assert not w.advance(),'Idle scheduler created a model call'
    s=snapshot();assert len(s['jobs'])==count
    idle.append({'label':label,'polls_after_restart':30,'jobs':count,'state':s['state'],
                 'deliveries':deepcopy(s['deliveries']),'dependencies':deepcopy(s.get('internal_requests',[]))})
    atomic_json(base/'idle.json',idle)
    print('STABLE',label,s['state'],count,flush=True)

if not idle:
    drain();stable('source delivered; deployment unknown')
answers=[('deployment','部署环境为单机 Windows 桌面，必须离线使用；这是合成验收输入。导出格式尚未给出。'),
         ('format','导出格式选 JSON，UTF-8 编码；这是合成验收输入，当前需求已明确。')]
for name,answer in answers:
    if any(i['label']==name for i in idle):continue
    s=snapshot();questions=[q for q in s['questions'].values() if q['state']=='open' and q.get('blocking')]
    assert questions,('expected actual user gate',s['state'],list(s['questions'].values()))
    # Answer the current genuinely issued question, never forge a question or
    # a model result. Additional independent questions fail this test for audit.
    q=questions[0]
    w.input(answer,question_id=q['id'],source='fixture',profile_scope='none',submission_id='acceptance-'+name)
    drain();stable(name)
s=snapshot()
assert s['state'] in {'delivered','quiescent'},s['state']
assert any(r['state']=='consumed' and r['response']['artifacts'] for r in s.get('internal_requests',[]))
assert any(d['state']=='partial' for d in s['deliveries'])
for request in s.get('internal_requests',[]):
    if request.get('input_id'):
        delivery=next(i for i in s['user_inputs'] if i['id']==request['input_id'])
        for artifact in delivery.get('artifacts',[]):
            assert artifact['sha256']==hashlib.sha256(artifact['content'].encode('utf-8')).hexdigest()
assert all(c['model']=='gpt-6-astra' and c['effort']=='xhigh' for c in calls)
atomic_json(base/'acceptance.json',{'calls':calls,'idle':idle,'final_state':s['state'],
    'boundary':'Synthetic setup/answers; all cognitive outputs are actual isolated Astra/xhigh calls. No production exploration cutoff.'})
print('VERIFIED',base,flush=True)
service.close()
