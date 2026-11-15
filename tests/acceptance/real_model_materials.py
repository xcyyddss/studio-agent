"""Actual full-source preparation, coverage review, council and parent uptake."""
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT)]
from studio_runtime.workflow import Workflow
from studio_runtime.service import StudioService
from studio_runtime.provider import CodexProvider
from studio_runtime.contracts import atomic_json

base=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else ROOT/'.validation'/('materials-'+uuid.uuid4().hex[:8])
assert base.is_relative_to((ROOT/'.validation').resolve())
base.mkdir(parents=True,exist_ok=True)
service=StudioService(base)
goal=('合成验收：依据完整材料为一个离线归档工具形成有依据的设计取舍与验收说明。材料中的四个明确约束必须保留，'
      '普通重复描述不应挤掉少数重要边界；已有信息足够则交付，不需要向用户提问或实现软件。'
      '议会可以发现遗漏或重问；父作者可本地处理明确的小型增量，有限核对不需要再次进行开放候选生成。')
if not (base/'run'/'workflow.db').exists():
    service.create(goal,run_id='run',web_search='disabled')
    w=Workflow(base/'run')
    # Repetitive records deliberately surround rare critical conditions.
    rows=['READ_ONLY=true; 禁止修改源文件。']
    for n in range(650):
        if n==170: rows.append('RETENTION_DAYS=17; 到期只清理缓存，不删除源文件。')
        if n==430: rows.append('EXPORT_FORMAT=JSON_UTF8; 导出必须保留来源路径与版本哈希。')
        rows.append(f'记录{n:04d}: 普通观察。界面应让操作者看到来源与处理进度；状态展示是操作记录，不等于结果正确。' * 3)
    rows.append('NETWORK=disabled; 本阶段无外部服务依赖。')
    material='\n'.join(rows)
    with w._tx() as (c,s):
        s['paused']=True
        for j in s['jobs'].values():j['state']='superseded'
        for r in s['inquiries'].values():r['state']='closed'
        s['jobs']['FIXTURE.SOURCE']={'state':'applied','created_at':'setup','spec':{'job_id':'FIXTURE.SOURCE','role':'parent','metadata':{'topic_id':'ROOT'}}}
        c.execute('INSERT INTO artifacts VALUES(?,?,?)',('artifacts/FIXTURE.SOURCE/requirements.txt',material,'FIXTURE.SOURCE'))
        w._schedule_intent(s,'inquiry','ROOT',question='根据 artifacts/FIXTURE.SOURCE/requirements.txt 的完整内容提出有价值的问题或设计取舍；保留全部明确约束及边界。')
        s['paused']=False
else:w=Workflow(base/'run')
calls=json.loads((base/'calls.json').read_text(encoding='utf8')) if (base/'calls.json').exists() else []
active={};provider=CodexProvider({'web_search':'disabled'})
with ThreadPoolExecutor(max_workers=6) as pool:
    while True:
        service._execute_tools(base/'run',w)
        w.repair_contract_failures();w.recover_failures()
        jobs=w.advance();busy={j.job_id for j in active.values()}
        for job in jobs:
            if job.job_id in busy or len(active)>=6:continue
            aid='real-'+uuid.uuid4().hex
            active[pool.submit(provider.execute,job,aid,base/'run'/'attempts'/aid)]=job
            print('CALL',job.job_id,job.role,len(job.prompt),flush=True)
        if not active:
            with w._read_tx() as (_,s): state=deepcopy(s)
            assert not [(k,j.get('error')) for k,j in state['jobs'].items() if j['state']=='failed']
            assert state['state'] in {'delivered','quiescent'},(state['state'],state['state_reason'])
            break
        done,_=wait(active,timeout=1,return_when=FIRST_COMPLETED)
        for future in done:
            job=active.pop(future);r=future.result();r['result_id']=r['attempt_id']
            t=time.monotonic();outcome=w.apply(r,defer_progress=True)
            calls.append({'job':job.job_id,'role':job.role,'outcome':r['outcome'],'import':outcome,
                'import_seconds':time.monotonic()-t,'chars':len(job.prompt),'model':r['receipt'].get('model'),'effort':r['receipt'].get('reasoning_effort')})
            atomic_json(base/'calls.json',calls);print('RESULT',json.dumps(calls[-1]),flush=True)
preparations=state.get('council_preparations',{})
assert len(preparations)==2,preparations
for mid,record in preparations.items():
    material=w.parliament.inspect(mid)
    for text in ['READ_ONLY','RETENTION_DAYS','EXPORT_FORMAT','NETWORK']:
        assert text in material['content'],(mid,'critical field lost',text)
    for uid in record['memory_unit_ids']:
        assert state['context_memory']['units'][uid]['state']=='accepted'
    consumers=[j for j in state['jobs'].values() if j['spec']['role']=='member' and mid in j['spec']['metadata'].get('evidence_material_ids',[])]
    assert consumers and all(j['state']=='applied' for j in consumers)
executed=[r for r in state['inquiries'].values() if r.get('jobs') and any(state['jobs'][j]['state']=='applied' for j in r['jobs'])]
assert executed and all(r.get('uptake') for r in executed)
count=len(state['jobs']);w=Workflow(base/'run')
for _ in range(30):assert w.advance()==[]
with w._read_tx() as (_,s):assert len(s['jobs'])==count
atomic_json(base/'acceptance.json',{'calls':calls,'preparations':preparations,'state':state['state'],
    'stable_polls':30,'critical_fields_checked':['READ_ONLY','RETENTION_DAYS','EXPORT_FORMAT','NETWORK'],
    'boundary':'Synthetic material; actual isolated preparation, independent coverage review, council generation and parent uptake. Assertions do not certify all semantic details.'})
print('VERIFIED',base,flush=True)
service.close()
